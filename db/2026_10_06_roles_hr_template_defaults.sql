-- N-01 (user decision): keep the code-template "HR / Recruitment Staff"
-- role and add only HR's documented gaps. The one matrix change is
-- Travel & Expense Approval = 'v' (read-only, company-wide expense /
-- travel lists -- crud.get_visible_employee_ids_for_requests treats 'v' as
-- company-wide view, while deciding still needs e/a). Org-wide leave
-- balances, document upload and exit letters are code fixes (M-10:
-- HR-representative narrowing now falls back to HR's People scope).
--
-- Applied to _template and to every HRMS tenant whose HR role STILL
-- matches the old code template exactly (rbac_columns.DEFAULT_MATRIX
-- before this change). A hand-customised HR role (impacgo-solutions) is
-- left untouched. Idempotent.

DO $$
DECLARE
  s text;
  perm uuid;
  n int;
  old_tpl constant text :=
    'benefits_admin=e,leave_approval=v,own_profile=s,payroll_process=e,performance_reviews=e,'
    'projects_access=e,recruitment=a,reports_analytics=e,team_attendance=v';
BEGIN
  FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
    IF to_regclass(format('%I.core_roles', s)) IS NULL THEN CONTINUE; END IF;
    EXECUTE format($q$SELECT id FROM %1$I.core_permissions
                     WHERE resource = 'travel_expense_approval' AND action = 'v' ORDER BY code LIMIT 1$q$, s)
      INTO perm;
    IF perm IS NULL THEN
      perm := gen_random_uuid();
      EXECUTE format($q$INSERT INTO %1$I.core_permissions (id, code, module, resource, action)
                       VALUES ($1, 'travel_expense_approval.v', 'travel', 'travel_expense_approval', 'v')$q$, s)
        USING perm;
    END IF;
    EXECUTE format($q$
      INSERT INTO %1$I.core_role_permissions (role_id, permission_id)
      SELECT r.id, $1 FROM %1$I.core_roles r
      WHERE r.name = 'HR / Recruitment Staff'
        AND (SELECT string_agg(p.resource || '=' || p.action, ',' ORDER BY p.resource)
             FROM %1$I.core_role_permissions rp JOIN %1$I.core_permissions p ON p.id = rp.permission_id
             WHERE rp.role_id = r.id AND p.action IN ('v','s','e','a')
               AND p.resource IN ('own_profile','team_attendance','leave_approval','timesheet_approval',
                                  'payroll_view_own','payroll_process','benefits_admin','recruitment',
                                  'performance_reviews','org_structure_config','reports_analytics',
                                  'system_settings_rbac','audit_logs','travel_expense_approval',
                                  'projects_access','asset_management')) = $2
      ON CONFLICT DO NOTHING
    $q$, s) USING perm, old_tpl;
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '%: HR role(s) given Travel & Expense view: %', s, n;
  END LOOP;
END $$;
