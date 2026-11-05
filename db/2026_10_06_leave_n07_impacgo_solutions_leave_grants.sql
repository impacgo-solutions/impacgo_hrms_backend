-- N-07 (impacgo-solutions data fix, authorised by the user) -- idempotent.
--
-- impacgo-solutions' built-in Manager, General Manager / Sr. Manager and
-- Project Manager roles were hand-granted the granular leave.approve /
-- leave.reject actions, which used to let them decide ANY employee's leave
-- (routers/leave.py, routers/leave_withdrawals.py, approval inbox). The code
-- no longer honours a granular action alone (only the reporting chain or an
-- org-wide leave administrator -- Owner / Admin on Leave Approval -- decides),
-- so these grants are removed to match the template roles (which only hold
-- leave.view) and so the Roles screen doesn't show a power the roles don't
-- have. Managers keep deciding their own reports' leave via the reporting
-- chain. Only touches impacgo-solutions, and only if it is an HRMS schema.
DO $$
DECLARE
    s CONSTANT text := 'impacgo-solutions';
    n int;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_temp.hrms_schemas() h WHERE h = s) THEN
        RAISE NOTICE '% is not an HRMS schema here -- nothing to do', s;
        RETURN;
    END IF;
    EXECUTE format(
        'DELETE FROM %I.core_role_permissions rp
          USING %I.core_roles r, %I.core_permissions p
          WHERE rp.role_id = r.id AND rp.permission_id = p.id
            AND r.name IN (''Manager'', ''General Manager / Sr. Manager'', ''Project Manager'')
            AND p.resource = ''leave'' AND p.action IN (''approve'', ''reject'')', s, s, s);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '%: removed % leave approve/reject grant(s) from Manager / GM / Project Manager', s, n;
END $$;
