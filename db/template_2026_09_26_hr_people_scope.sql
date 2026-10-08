-- template_2026_09_26_hr_people_scope.sql
-- New tenants: HR / Recruitment Staff gets People scope 'org-full' (whole
-- organisation + Onboarding / Offboarding / Transfers & Promotions tabs),
-- matching impacgo-solutions. Touches ONLY the _template schema's template
-- company; app/provision_tenant.py copies role scopes into each new tenant.
-- Existing tenants are not changed. Idempotent.
UPDATE _template.core_roles
   SET people_scope = 'org-full'
 WHERE company_id = '11111111-1111-1111-1111-111111111111'
   AND name = 'HR / Recruitment Staff';
