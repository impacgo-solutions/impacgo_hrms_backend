-- ============================================================================
-- RBAC template provisioning functions -- REVIEW AND RUN BY HAND, once, to
-- install these. Calling them (which this file does NOT do) is what
-- actually writes into a tenant schema -- see backend/app/provision_tenant.py
-- (new tenants) and backend/app/migrate_apply_rbac_template.py (existing
-- tenants).
-- ============================================================================
--
-- Copies the RBAC default template (backend/db/seed_rbac_template.sql, the
-- sentinel row in _template.core_companies) into a REAL tenant schema under
-- a REAL company_id/role_id. Matches roles by NAME (public.provision_tenant_
-- rbac) or targets an explicit existing role_id directly (public.
-- apply_template_role_grants, for tenants whose equivalent role has a
-- different, custom name -- see that function's own comment). Permissions
-- are always matched by CODE (fresh UUIDs generated per tenant) -- this
-- table pair has no DB unique constraint on either despite the ORM
-- declaring one (see crud.get_or_create_permission's own check-then-insert
-- precedent, mirrored here in SQL).
--
-- ADDITIVE, PER-RESOURCE, BY CONSTRUCTION -- this is the important fix over
-- an earlier version of this file:
--
--   Both functions below apply the template's grants ONE RESOURCE AT A TIME
--   (a "resource" is a legacy matrix column like leave_approval, or a
--   granular catalog resource like employee). For each resource the
--   template defines any grant for, on a given role:
--     * if the tenant's role ALREADY has ANY grant for that resource
--       (whatever level/actions it happens to be) -- SKIP it entirely,
--       leaving the tenant's existing choice completely untouched.
--     * if the tenant's role has ZERO grants for that resource -- apply the
--       template's FULL default for it.
--
--   Why per-resource, not per-permission-code: the legacy matrix stores
--   "one level per column" as a single core_role_permissions row (e.g.
--   leave_approval.e). A naive "insert this permission row if its CODE
--   doesn't already exist" check (what the previous version of this
--   function did) cannot detect that a role already has leave_approval SET
--   AT A DIFFERENT LEVEL (e.g. leave_approval.v) -- .e and .v are different
--   codes, so the old check would happily insert leave_approval.e
--   alongside the tenant's existing leave_approval.v, leaving the role with
--   BOTH grants linked simultaneously. crud.build_role_matrix then picks
--   whichever one SQLAlchemy happens to iterate last -- undefined,
--   effectively random behavior, and a real risk of silently overriding a
--   tenant's deliberate customization. Skipping the whole RESOURCE the
--   moment any existing grant for it is found closes that gap and is the
--   correct, safe reading of "preserve any tenant-specific customizations
--   already made": a tenant that has touched a resource at all, in any way,
--   keeps exactly what they have; only resources the tenant's role has
--   never configured pick up the template default.
--
-- Safe to re-run for the same tenant/role any number of times (idempotent):
-- a resource already applied is already "not empty", so a second run finds
-- nothing new to add for it.
--
-- Uses EXECUTE format(...) with every value passed via USING (never
-- string-interpolated) -- the same injection-safe pattern
-- public.provision_tenant() itself already uses for %I identifiers.


-- ----------------------------------------------------------------------------
-- Shared helper: apply one template role's grants onto one tenant role,
-- resource by resource, skipping any resource the tenant role already has
-- ANY grant for. Used by both public functions below -- not meant to be
-- called directly (hence the leading underscore, matching this repo's own
-- convention for internal-only SQL helpers... there isn't one yet, but it
-- mirrors Python's `_private` convention used throughout backend/app/).
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public._apply_template_role_resource_grants(
    p_tenant_slug text,
    p_tenant_role_id uuid,
    p_template_role_id uuid,
    p_role_is_new boolean  -- true when the caller just created p_tenant_role_id
                           -- in this same call -- skips the "does it already
                           -- have a grant for this resource" check entirely,
                           -- since a brand-new role can't have one yet, and
                           -- saves a query per resource in the common case.
) RETURNS TABLE(perms_created integer, grants_created integer, resources_skipped integer)
LANGUAGE plpgsql
AS $$
DECLARE
    tmpl_resource   RECORD;
    tmpl_perm       RECORD;
    tenant_perm_id  uuid;
    has_existing    boolean;
    v_perms_created integer := 0;
    v_grants_created integer := 0;
    v_resources_skipped integer := 0;
    row_count_tmp   integer;
BEGIN
    FOR tmpl_resource IN
        SELECT DISTINCT p.resource
        FROM _template.core_role_permissions rp
        JOIN _template.core_permissions p ON p.id = rp.permission_id
        WHERE rp.role_id = p_template_role_id
        ORDER BY p.resource
    LOOP
        has_existing := false;
        IF NOT p_role_is_new THEN
            EXECUTE format(
                'SELECT EXISTS (
                     SELECT 1
                     FROM %I.core_role_permissions rp
                     JOIN %I.core_permissions p ON p.id = rp.permission_id
                     WHERE rp.role_id = $1 AND p.resource = $2
                 )',
                p_tenant_slug, p_tenant_slug
            ) INTO has_existing USING p_tenant_role_id, tmpl_resource.resource;
        END IF;

        IF has_existing THEN
            v_resources_skipped := v_resources_skipped + 1;
            CONTINUE;  -- tenant already has an explicit grant for this
                       -- resource (any level/action) -- leave it alone.
        END IF;

        FOR tmpl_perm IN
            SELECT p.code, p.module, p.resource, p.action
            FROM _template.core_role_permissions rp
            JOIN _template.core_permissions p ON p.id = rp.permission_id
            WHERE rp.role_id = p_template_role_id AND p.resource = tmpl_resource.resource
        LOOP
            EXECUTE format('SELECT id FROM %I.core_permissions WHERE code = $1', p_tenant_slug)
                INTO tenant_perm_id
                USING tmpl_perm.code;

            IF tenant_perm_id IS NULL THEN
                tenant_perm_id := gen_random_uuid();
                EXECUTE format(
                    'INSERT INTO %I.core_permissions (id, code, module, resource, action) VALUES ($1, $2, $3, $4, $5)',
                    p_tenant_slug
                ) USING tenant_perm_id, tmpl_perm.code, tmpl_perm.module, tmpl_perm.resource, tmpl_perm.action;
                v_perms_created := v_perms_created + 1;
            END IF;

            EXECUTE format(
                'INSERT INTO %I.core_role_permissions (role_id, permission_id) VALUES ($1, $2) ON CONFLICT (role_id, permission_id) DO NOTHING',
                p_tenant_slug
            ) USING p_tenant_role_id, tenant_perm_id;
            GET DIAGNOSTICS row_count_tmp = ROW_COUNT;
            v_grants_created := v_grants_created + row_count_tmp;
        END LOOP;
    END LOOP;

    RETURN QUERY SELECT v_perms_created, v_grants_created, v_resources_skipped;
END;
$$;


-- ----------------------------------------------------------------------------
-- public.provision_tenant_rbac(tenant_slug, company_id)
-- ----------------------------------------------------------------------------
-- Ensures all 14 built-in template roles exist in the tenant (by NAME) and
-- applies the template's per-resource defaults to each, skipping any
-- resource already configured. Use this for:
--   * brand-new tenants (every role is missing -> all 14 get created with
--     the full template default, since a freshly created role has nothing
--     to skip), and
--   * existing tenants whose roles already use the exact built-in names
--     ("Organization Owner / CEO", "HR / Recruitment Staff", etc.) -- any
--     of those 14 names missing gets created; any that already exist gain
--     only the resources they haven't already configured.
--
-- Does NOT help a tenant whose equivalent role has a custom name (e.g. an
-- "HR Manager" role instead of "HR / Recruitment Staff") -- for that, see
-- public.apply_template_role_grants below.

CREATE OR REPLACE FUNCTION public.provision_tenant_rbac(p_tenant_slug text, p_company_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    template_company_id CONSTANT uuid := '11111111-1111-1111-1111-111111111111';
    tmpl_role     RECORD;
    tenant_role_id uuid;
    role_is_new   boolean;
    result        RECORD;
    roles_created integer := 0;
    total_perms   integer := 0;
    total_grants  integer := 0;
    total_skipped integer := 0;
BEGIN
    FOR tmpl_role IN
        SELECT id, name, description
        FROM _template.core_roles
        WHERE company_id = template_company_id
        ORDER BY name
    LOOP
        EXECUTE format('SELECT id FROM %I.core_roles WHERE company_id = $1 AND name = $2', p_tenant_slug)
            INTO tenant_role_id
            USING p_company_id, tmpl_role.name;

        role_is_new := (tenant_role_id IS NULL);
        IF role_is_new THEN
            tenant_role_id := gen_random_uuid();
            EXECUTE format(
                'INSERT INTO %I.core_roles (id, company_id, name, description, is_system) VALUES ($1, $2, $3, $4, true)',
                p_tenant_slug
            ) USING tenant_role_id, p_company_id, tmpl_role.name, tmpl_role.description;
            roles_created := roles_created + 1;
        END IF;

        SELECT * INTO result
        FROM public._apply_template_role_resource_grants(p_tenant_slug, tenant_role_id, tmpl_role.id, role_is_new);
        total_perms := total_perms + result.perms_created;
        total_grants := total_grants + result.grants_created;
        total_skipped := total_skipped + result.resources_skipped;
    END LOOP;

    RAISE NOTICE 'provision_tenant_rbac(%, %): % role(s) created, % permission(s) created, % grant(s) added, % resource(s) skipped (already customized by this tenant)',
        p_tenant_slug, p_company_id, roles_created, total_perms, total_grants, total_skipped;
END;
$$;


-- ----------------------------------------------------------------------------
-- public.apply_template_role_grants(tenant_slug, target_role_id, template_role_name)
-- ----------------------------------------------------------------------------
-- Opt-in mapping for tenants whose equivalent of a built-in role has a
-- CUSTOM name -- e.g. a tenant's real HR role is called "HR Manager", not
-- "HR / Recruitment Staff". Unlike provision_tenant_rbac, this NEVER
-- creates a role -- it only ever grants missing resources onto a role
-- id you explicitly name, because guessing "this custom-named role is
-- probably the HR one" from name similarity alone is exactly the kind of
-- silent misapplication of broad permissions that must never happen
-- automatically. You (the operator, who knows what "HR Manager" actually
-- means in this specific tenant) decide the mapping; this function only
-- executes it, using the identical per-resource skip-if-customized logic as
-- provision_tenant_rbac -- so it's just as safe to re-run, and just as
-- unable to disturb any resource that role has already been configured for.
--
-- Example (run by hand, after finding the role's id via
-- `SELECT id, name FROM infyq.core_roles;`):
--   SELECT public.apply_template_role_grants(
--       'infyq', '<HR Manager''s role id>', 'HR / Recruitment Staff'
--   );

CREATE OR REPLACE FUNCTION public.apply_template_role_grants(
    p_tenant_slug text,
    p_target_role_id uuid,
    p_template_role_name text
) RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    template_company_id CONSTANT uuid := '11111111-1111-1111-1111-111111111111';
    tmpl_role_id uuid;
    target_role_exists boolean;
    result RECORD;
BEGIN
    SELECT id INTO tmpl_role_id
    FROM _template.core_roles
    WHERE company_id = template_company_id AND name = p_template_role_name;

    IF tmpl_role_id IS NULL THEN
        RAISE EXCEPTION 'No template role named % found in _template (check backend/app/rbac_columns.py BUILTIN_ROLES for the exact spelling)',
            p_template_role_name;
    END IF;

    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.core_roles WHERE id = $1)', p_tenant_slug)
        INTO target_role_exists
        USING p_target_role_id;

    IF NOT target_role_exists THEN
        RAISE EXCEPTION 'Role % not found in tenant %.core_roles -- this function only grants onto an EXISTING role, it never creates one',
            p_target_role_id, p_tenant_slug;
    END IF;

    SELECT * INTO result
    FROM public._apply_template_role_resource_grants(p_tenant_slug, p_target_role_id, tmpl_role_id, false);

    RAISE NOTICE 'apply_template_role_grants(%, %, %): % permission(s) created, % grant(s) added, % resource(s) skipped (already customized)',
        p_tenant_slug, p_target_role_id, p_template_role_name,
        result.perms_created, result.grants_created, result.resources_skipped;
END;
$$;


-- ----------------------------------------------------------------------------
-- Verification (read-only) -- run after calling either function, e.g.:
--   SELECT public.provision_tenant_rbac('acme', '<some core_companies.id>');
--   SELECT public.apply_template_role_grants('infyq', '<hr manager role id>', 'HR / Recruitment Staff');
-- ----------------------------------------------------------------------------
-- SELECT name, is_system FROM acme.core_roles WHERE company_id = '<company_id>' ORDER BY name;
-- SELECT r.name, p.resource, p.action FROM infyq.core_roles r
--   JOIN infyq.core_role_permissions rp ON rp.role_id = r.id
--   JOIN infyq.core_permissions p ON p.id = rp.permission_id
--   WHERE r.id = '<hr manager role id>' ORDER BY p.resource, p.action;
