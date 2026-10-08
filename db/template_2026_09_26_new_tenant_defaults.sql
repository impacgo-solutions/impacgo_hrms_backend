-- template_2026_09_26_new_tenant_defaults.sql
--
-- Default configuration every NEW tenant inherits at provisioning
-- (app/provision_tenant.py copies these rows from the _template schema's
-- template company into the new company, with fresh ids). Values are the
-- tenant-agnostic configuration of the reference tenant (impacgo-solutions)
-- as of 2026-09-26: bands, leave types, salary components, the general
-- shift, the Email/SMTP integration entry (disconnected), the payslip and
-- offer-letter HTML templates (placeholders only), and the approval
-- workflow definitions (dynamic approver types, no people).
-- No employees, projects, holidays, customers, credentials or other
-- business records. Touches ONLY the _template schema; idempotent.
BEGIN;

-- core_bands: 10 row(s)
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('6fa95943-9d87-58fa-8130-2362c2aa9beb', '11111111-1111-1111-1111-111111111111', 'Ownership / Founding', 'BAND-01', 1, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('6ecc683b-10ad-55d0-b570-3deceeed059a', '11111111-1111-1111-1111-111111111111', 'C-Level Executive', 'BAND-02', 2, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('72a62e44-a12c-50b2-bb67-65aada4ab0ea', '11111111-1111-1111-1111-111111111111', 'VP Level', 'BAND-03', 3, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('6c1b003d-3789-5e30-b601-39ddc2056864', '11111111-1111-1111-1111-111111111111', 'Director Level', 'BAND-04', 4, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('49649c48-453b-5c0f-a5cf-39ed341a2013', '11111111-1111-1111-1111-111111111111', 'General Management', 'BAND-05', 5, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('18290993-9824-5b30-b380-27c6cff06da3', '11111111-1111-1111-1111-111111111111', 'Management', 'BAND-06', 6, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('5d2a545b-1e50-52dd-976b-941eb38c661d', '11111111-1111-1111-1111-111111111111', 'Team Lead', 'BAND-07', 7, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('b712726b-0c2b-5dab-afd8-85a174149cfd', '11111111-1111-1111-1111-111111111111', 'Professional / IC (Senior)', 'BAND-08', 8, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('a8b14b71-dcfd-54fe-8439-a31817f644bc', '11111111-1111-1111-1111-111111111111', 'Professional / IC', 'BAND-09', 9, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_bands (id, company_id, name, code, band_number, description, is_active, created_by, created_at, updated_by, updated_at) VALUES ('ea3f8435-c4a8-56e3-b02e-ebc1b64075e2', '11111111-1111-1111-1111-111111111111', 'Associate / Entry Level', 'BAND-10', 10, NULL, true, NULL, now(), NULL, now()) ON CONFLICT DO NOTHING;

-- hcm_leave_types: 6 row(s)
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('ba67c44f-9343-5315-ab74-ecc5efc83c52', '11111111-1111-1111-1111-111111111111', 'Bereavement Leave', 'BL', true, 5.0, false, false) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('f95eeddc-0518-5521-9824-f168f76dbd47', '11111111-1111-1111-1111-111111111111', 'Casual Leave', 'CL', true, NULL, false, false) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('e73b7b6c-5e36-5378-ba47-127a74cf7e6a', '11111111-1111-1111-1111-111111111111', 'Compensatory Off', 'CO', true, NULL, false, false) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('07b4d35e-5a59-5a31-b931-3517fb48dc6b', '11111111-1111-1111-1111-111111111111', 'Earned Leave', 'EL', true, 10.0, false, false) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('6abc3d96-2472-56f9-aec4-854bebae4a50', '11111111-1111-1111-1111-111111111111', 'Loss of Pay (LOP)', 'LOP', false, NULL, false, false) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_leave_types (id, company_id, name, code, is_paid, max_days_per_year, carry_forward, is_encashable) VALUES ('4fd44489-769b-506b-9933-1949e4e5fab6', '11111111-1111-1111-1111-111111111111', 'Sick Leave', 'SL', true, 6.0, false, false) ON CONFLICT DO NOTHING;

-- hcm_salary_components: 9 row(s)
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('0f4a1bbf-330c-5c6c-98bf-3fd2f90ca6a0', '11111111-1111-1111-1111-111111111111', 'Asset Recovery Deduction', 'ASSET-DED', 'deduction', 'flat', NULL, false, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('154c3f49-080b-5d7b-be11-ecdb4cec2ed4', '11111111-1111-1111-1111-111111111111', 'Basic', 'BASIC', 'earning', 'percent', NULL, true, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('e5118890-ef8e-5e65-a372-7efb3fc9d1aa', '11111111-1111-1111-1111-111111111111', 'House Rent Allowance', 'HRA', 'earning', 'percent', NULL, true, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('3e7dddfd-cc55-5fc5-a7f7-1ee9633fe1c0', '11111111-1111-1111-1111-111111111111', 'Income Tax', 'INCTAX', 'tax', 'flat', NULL, false, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('ce65c815-8af1-50a7-911f-1ad3100ab5e6', '11111111-1111-1111-1111-111111111111', 'Loss of Pay (LOP)', 'LOP-DED', 'deduction', 'flat', NULL, false, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('29c36672-8ba8-5321-8cb9-6fb664117a9e', '11111111-1111-1111-1111-111111111111', 'Professional Tax', 'PT', 'deduction', 'flat', NULL, false, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('0227824f-6c2a-5161-b014-2390a4fe3329', '11111111-1111-1111-1111-111111111111', 'Special Allowance', 'SPCL', 'earning', 'percent', NULL, true, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('7c4105c3-fd5d-5078-8262-9be347803875', '11111111-1111-1111-1111-111111111111', 'TDS (Estimated)', 'TDS-EST', 'tax', 'flat', NULL, true, NULL, NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.hcm_salary_components (id, company_id, name, code, component_type, calc_type, formula, is_taxable, statutory_code, gl_account_id) VALUES ('6826f54e-582e-52bc-a22b-6532b5521405', '11111111-1111-1111-1111-111111111111', 'Variable Pay', 'VARPAY', 'earning', 'var_annual', NULL, true, NULL, NULL) ON CONFLICT DO NOTHING;

-- hcm_shifts: 1 row(s)
INSERT INTO _template.hcm_shifts (id, company_id, name, start_time, end_time, break_minutes, grace_minutes, is_night, is_active, max_breaks) VALUES ('0e4e4865-48d0-5c06-9edf-17ccdc1d8201', '11111111-1111-1111-1111-111111111111', 'General Shift', '09:00:00', '17:00:00', 60, 10, false, true, 1) ON CONFLICT DO NOTHING;

-- core_integrations: 1 row(s)
INSERT INTO _template.core_integrations (id, company_id, name, description, is_connected, updated_at) VALUES ('cd7f7b26-821a-5bd6-8f79-e8811e4d8247', '11111111-1111-1111-1111-111111111111', 'Email / SMTP', 'Payslips, offer letters, system mail', false, now()) ON CONFLICT DO NOTHING;

-- hcm_payslip_html_templates: 1 row(s)
INSERT INTO _template.hcm_payslip_html_templates (id, company_id, name, html_body, css_styles, is_active, version, updated_by, created_at, updated_at) VALUES ('8a546338-ff41-5c4a-8dd7-268e48e100f7', '11111111-1111-1111-1111-111111111111', 'Standard Payslip', '<div class="payslip">
  <div class="header">
    <div class="header-left">
      <h1>{{company_name}}</h1>
      <p class="address">{{company_address}}</p>
    </div>
    <div class="header-logo">
      <img src="{{company_logo}}" alt="Company Logo" />
    </div>
  </div>

  <h2 class="title">Payslip for the month of {{pay_period}}</h2>

  <h3 class="section-title">Employee Pay Summary</h3>

  <div class="summary">
    <div class="employee-details">
      <div class="field"><span class="label">Employee Name</span><span class="value bold">{{employee_name}}</span></div>
      <div class="field"><span class="label">Employee No</span><span class="value">{{employee_id}}</span></div>
      <div class="field"><span class="label">Designation</span><span class="value">{{designation}}</span></div>
      <div class="field"><span class="label">Department</span><span class="value">{{department}}</span></div>
      <div class="field"><span class="label">Date of Joining</span><span class="value">{{joining_date_ddmmyyyy}}</span></div>
      <div class="field"><span class="label">Pay Period</span><span class="value">{{pay_period}}</span></div>
      <div class="field"><span class="label">Pay Date</span><span class="value">{{pay_date_ddmmyyyy}}</span></div>
      <div class="field payment-details">
        <span class="label">Bank Name / Account No</span><span class="value">{{bank_name}} - {{bank_account_no}}</span>
        <span class="label payment-mode-label">Payment Mode</span><span class="value">{{payment_mode}}</span>
      </div>
    </div>
    <div class="net-pay">
      <div class="net-pay-label">Employee Net Pay</div>
      <div class="net-pay-value">&#8377;{{net_salary}}</div>
      <div class="paid-days">Paid Days : {{paid_days}} | LOP Days : {{lop_days}}</div>
    </div>
  </div>

  <table class="salary-table">
    <tr>
      <td class="earn-col">
        <table class="component-table">
          <tr class="table-head"><th>Earnings</th><th>Amount</th><th>YTD</th></tr>
          {{earnings_rows_ytd}}
          <tr class="table-foot"><td>Gross Earnings</td><td>{{gross_earnings}}</td><td>{{gross_earnings_ytd}}</td></tr>
        </table>
      </td>
      <td class="deduct-col">
        <table class="component-table">
          <tr class="table-head"><th>Deductions</th><th>Amount</th><th>YTD</th></tr>
          {{deductions_rows_ytd}}
          <tr class="table-foot"><td>Total Deductions</td><td>{{total_deductions}}</td><td></td></tr>
        </table>
      </td>
    </tr>
  </table>

  <div class="net-payable-band">
    Total Net Payable <span class="amount-big">&#8377; {{net_salary}}</span>
    <span class="in-words">({{net_payable_in_words}})</span>
  </div>
  <p class="note">**Total Net Payable = Gross Earnings - Total Deductions</p>

  <p class="footer">-- This is system generated payslip. --</p>
</div>
', '.payslip {
  font-family: "Segoe UI", Arial, Helvetica, sans-serif;
  color: #1f1f1f;
  font-size: 13px;
  max-width: 900px;
  margin: 0 auto;
  padding: 28px 32px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  border-bottom: 1px solid #d8d8d8;
  padding-bottom: 14px;
  margin-bottom: 18px;
}

.header-left h1 {
  font-size: 21px;
  font-weight: 700;
  margin: 0;
  letter-spacing: 0.2px;
}

.header-left .address {
  font-size: 11px;
  color: #555555;
  margin: 6px 0 0;
  max-width: 480px;
  line-height: 1.5;
}

.header-logo img {
  height: 42px;
  max-width: 180px;
  object-fit: contain;
}

.title {
  font-size: 16px;
  font-weight: 700;
  margin: 0 0 14px;
}

.section-title {
  font-size: 13px;
  font-weight: 700;
  margin: 0 0 10px;
}

.summary {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 24px;
  margin-bottom: 20px;
}

.employee-details {
  flex: 1;
}

.employee-details .field {
  display: flex;
  align-items: baseline;
  padding: 3px 0;
  font-size: 12.5px;
}

.employee-details .field .label {
  width: 190px;
  flex-shrink: 0;
  color: #666666;
}

.employee-details .field .value {
  color: #111111;
}

.employee-details .field .value.bold {
  font-weight: 700;
}

.employee-details .payment-details .payment-mode-label {
  width: 130px;
  margin-left: 24px;
  color: #666666;
}

.net-pay {
  border: 1px solid #dcdcdc;
  border-radius: 4px;
  padding: 16px 28px;
  text-align: center;
  min-width: 230px;
}

.net-pay-label {
  font-size: 11px;
  color: #666666;
  margin-bottom: 6px;
}

.net-pay-value {
  font-size: 28px;
  font-weight: 800;
  color: #1f9254;
  margin-bottom: 6px;
  white-space: nowrap;
}

.paid-days {
  font-size: 10.5px;
  color: #888888;
}

.salary-table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 4px;
}

.salary-table > tr > td {
  width: 50%;
  vertical-align: top;
  padding: 0;
}

.salary-table > tr > .earn-col {
  padding-right: 14px;
}

.salary-table > tr > .deduct-col {
  padding-left: 14px;
}

.component-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.component-table .table-head th {
  background: #f4f5f6;
  text-align: left;
  font-weight: 700;
  padding: 7px 8px;
  border-bottom: 1px solid #dddddd;
}

.component-table td {
  padding: 6px 8px;
  border-bottom: 1px solid #eeeeee;
}

.component-table .table-head th:nth-child(2),
.component-table .table-head th:nth-child(3),
.component-table td:nth-child(2),
.component-table td:nth-child(3) {
  text-align: right;
}

.component-table td:nth-child(2)::before,
.component-table td:nth-child(3):not(:empty)::before {
  content: "\20B9 ";
}

.component-table .table-foot td {
  font-weight: 700;
  border-top: 1px solid #b9b9b9;
  border-bottom: none;
  padding-top: 8px;
}

.net-payable-band {
  background: #eef6ec;
  border-left: 4px solid #4f9d5d;
  padding: 12px 18px;
  font-size: 13.5px;
  margin: 18px 0 6px;
}

.net-payable-band .amount-big {
  font-weight: 800;
  font-size: 17px;
}

.net-payable-band .in-words {
  font-weight: 400;
  font-size: 11px;
  color: #555555;
}

.note {
  font-size: 10px;
  color: #999999;
  margin: 4px 0 30px;
}

.footer {
  text-align: center;
  font-size: 10.5px;
  color: #999999;
  margin-top: 30px;
}
', true, 9, NULL, now(), now()) ON CONFLICT DO NOTHING;

-- hcm_offer_letter_html_templates: 1 row(s)
INSERT INTO _template.hcm_offer_letter_html_templates (id, company_id, name, html_body, css_styles, is_active, version, updated_by, created_at, updated_at) VALUES ('cad76f85-c107-548d-af45-3d8533241217', '11111111-1111-1111-1111-111111111111', 'Standard Offer Letter', '<div class="letter"><div class="header"><img class="logo" src="{{company_logo}}"/><div><h1>{{company_name}}</h1><p>{{company_address}}</p></div></div><h2>Offer Letter</h2><p>Date: {{current_date}}</p><p>Dear {{candidate_name}},</p><p>We are pleased to offer you the position of <strong>{{position}}</strong> in the <strong>{{department}}</strong> department at {{company_name}}, at our {{branch}} location.</p><table class="offer-details"><tr><td>Designation</td><td>{{designation}}</td></tr><tr><td>Employment Type</td><td>{{employment_type}}</td></tr><tr><td>Annual CTC</td><td>{{offered_ctc}} ({{offered_ctc_in_words}})</td></tr><tr><td>Proposed Joining Date</td><td>{{joining_date}}</td></tr><tr><td>Reporting Manager</td><td>{{reporting_manager}}</td></tr><tr><td>HR Contact</td><td>{{hr_contact}}</td></tr><tr><td>Offer Status</td><td>{{offer_status}}</td></tr></table><div class="signature"><p>We look forward to welcoming you to the team.</p><p>Sincerely,<br/>{{company_name}}</p></div></div>', '.letter{width:750px;margin:auto;padding:40px;font-family:Arial,sans-serif} .header{display:flex;align-items:center;gap:16px;margin-bottom:20px} .logo{width:60px;height:60px} table.offer-details{width:100%;border-collapse:collapse;margin:20px 0} table.offer-details td{border:1px solid #ccc;padding:8px}', true, 1, NULL, now(), now()) ON CONFLICT DO NOTHING;

-- core_approval_workflows: 9 row(s)
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('4768334e-d8a5-5b3f-9753-db4d824c6b70', '11111111-1111-1111-1111-111111111111', 'asset_request', 'Asset Request', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('d1ad9420-c564-58fd-b0fc-45fce7bd495e', '11111111-1111-1111-1111-111111111111', 'attendance_regularization', 'Attendance Regularization', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('aa286fef-937e-5ffa-92b4-544a5504a685', '11111111-1111-1111-1111-111111111111', 'exit_request', 'Resignation / Exit Request', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('19e9b7bf-ca84-57bb-8ce2-b3e8a9b5c3bb', '11111111-1111-1111-1111-111111111111', 'expense_claim', 'Reimbursement / Expense Report', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('2d4511fb-23ba-51f4-9e6c-69bcd395bcc5', '11111111-1111-1111-1111-111111111111', 'hiring_requisition', 'Hiring Requisition', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('fc8b1b51-1c3c-5f68-bc29-aea86803c406', '11111111-1111-1111-1111-111111111111', 'leave_request', 'Leave Request', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('c74f1151-7efa-5c81-9725-69162eb88ead', '11111111-1111-1111-1111-111111111111', 'salary_revision_request', 'Salary Revision', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('753528a9-5a97-58d2-971c-af0732d3872f', '11111111-1111-1111-1111-111111111111', 'timesheet', 'Timesheet Submission', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflows (id, company_id, doctype, name, is_active, created_at, created_by, updated_at, updated_by) VALUES ('53e0753e-fb79-5aa6-9ca5-f678df797bde', '11111111-1111-1111-1111-111111111111', 'travel_request', 'Travel Request', true, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;

-- core_approval_workflow_steps: 18 row(s)
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('9ac2a2bf-a386-5f9a-8b62-564fbd3e7436', 'fc8b1b51-1c3c-5f68-bc29-aea86803c406', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('d09c337e-2d26-50c7-a9ca-ded5cafe779d', 'fc8b1b51-1c3c-5f68-bc29-aea86803c406', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('12bb81b9-983b-5895-a40d-347f79daf950', 'aa286fef-937e-5ffa-92b4-544a5504a685', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('ea52261c-0d86-5c9c-b322-38847480a033', 'aa286fef-937e-5ffa-92b4-544a5504a685', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('0561efcf-bfcd-57b6-9728-dc42f52f6cef', '2d4511fb-23ba-51f4-9e6c-69bcd395bcc5', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('5de2c35f-9b4b-596d-b6e2-d633b4626ff4', '2d4511fb-23ba-51f4-9e6c-69bcd395bcc5', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('816936fe-166d-5094-965c-71c1cda9992d', 'c74f1151-7efa-5c81-9725-69162eb88ead', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('f9dbfd06-5181-5ea6-9764-eb715f39653a', 'c74f1151-7efa-5c81-9725-69162eb88ead', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('c6db0e20-71cb-5f56-807e-7a1bc3fd2f71', '19e9b7bf-ca84-57bb-8ce2-b3e8a9b5c3bb', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('27dc4c40-f3a0-5d95-bfcd-e54884b0f399', '19e9b7bf-ca84-57bb-8ce2-b3e8a9b5c3bb', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('e0a6b1bc-1aca-5e3d-b9a3-3fafb445db2f', '53e0753e-fb79-5aa6-9ca5-f678df797bde', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('bf11425f-b779-5ee0-91a1-34a8d22e4ba8', '53e0753e-fb79-5aa6-9ca5-f678df797bde', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('18a82bd1-f7df-5c0b-bd45-dc7d33ca44fd', '4768334e-d8a5-5b3f-9753-db4d824c6b70', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('dbd4d3a3-1c45-534c-99eb-6829adbff0c8', '4768334e-d8a5-5b3f-9753-db4d824c6b70', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('6a117b43-37e9-5b3e-aeea-1f092f8c7055', 'd1ad9420-c564-58fd-b0fc-45fce7bd495e', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('22aaf78a-dac6-5eac-9c55-4969dcba9682', 'd1ad9420-c564-58fd-b0fc-45fce7bd495e', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('a3f1d89a-61b0-567f-bc05-4c941bd7df5d', '753528a9-5a97-58d2-971c-af0732d3872f', 1, 'dotted_line_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;
INSERT INTO _template.core_approval_workflow_steps (id, workflow_id, step_order, approver_type, role_id, user_id, min_amount, max_amount, created_at, created_by, updated_at, updated_by) VALUES ('a3008edb-4f7d-5aa9-a08a-e62e3d763050', '753528a9-5a97-58d2-971c-af0732d3872f', 1, 'reporting_manager', NULL, NULL, NULL, NULL, now(), NULL, now(), NULL) ON CONFLICT DO NOTHING;

COMMIT;
