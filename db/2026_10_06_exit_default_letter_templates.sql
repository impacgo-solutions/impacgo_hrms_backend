-- N-02: default Experience & Relieving Letter and Full & Final Settlement
-- Statement templates, so letter generation works in a new tenant without
-- HR first writing one (routers/exit_letters._active_template returned 400).
--
--   * _template: one active template per letter type for the template
--     company -- provision_tenant.CONFIG_TEMPLATE_TABLES now includes
--     hcm_exit_letter_html_templates, so every NEW tenant gets copies.
--   * Existing HRMS tenants: every company with NO template of that type
--     (active or not) gets the default; companies that already have one
--     (e.g. impacgo-solutions' own design) are left untouched.
-- Placeholders only (exit_letter_html_renderer) -- no tenant data. The
-- authorised signatory is left empty for HR to set.
-- Idempotent.

CREATE OR REPLACE FUNCTION pg_temp.exr_html() RETURNS text LANGUAGE sql IMMUTABLE AS $f$ SELECT $html$<div class="letter">
  <header class="letterhead">
    <img class="logo" src="{{company_logo}}" alt="">
    <div class="company">
      <div class="company-name">{{company_legal_name}}</div>
      <div class="company-line">{{company_address}}</div>
      <div class="company-line">{{company_registration}}</div>
    </div>
  </header>

  <div class="meta">
    <span>Ref. No.: <strong>{{letter_number}}</strong></span>
    <span>Date: <strong>{{issue_date_long}}</strong></span>
  </div>

  <h1>Experience &amp; Relieving Letter</h1>

  <p>This is to certify that <strong>{{employee_salutation}} {{employee_name}}</strong>
  (Employee ID: <strong>{{employee_id}}</strong>) was employed with
  <strong>{{company_legal_name}}</strong> from <strong>{{date_of_joining_long}}</strong>
  to <strong>{{last_working_day_long}}</strong>. At the time of leaving the organisation,
  {{pronoun_subject}} held the position of <strong>{{designation}}</strong>. The total period
  of service with us is {{tenure}}.</p>

  <p>During {{pronoun_possessive}} tenure with us, {{pronoun_possessive}} conduct was found
  to be good and {{pronoun_subject}} carried out {{pronoun_possessive}} responsibilities
  sincerely.</p>

  <p>{{employee_name}} tendered {{pronoun_possessive}} resignation on
  {{resignation_date_long}}, which was accepted by the management. Having served the
  notice period and completed the handover and exit formalities,
  {{pronoun_subject}} is relieved from {{pronoun_possessive}} duties and responsibilities
  with effect from the close of business hours on
  <strong>{{relieving_date_long}}</strong>. {{full_and_final_statement}}</p>

  <p>We thank {{pronoun_object}} for {{pronoun_possessive}} contribution to the
  organisation and wish {{pronoun_object}} every success in {{pronoun_possessive}} future
  endeavours.</p>

  <div class="signature">
    <p>For <strong>{{company_legal_name}}</strong></p>
    <div class="sign-area">
      <img class="sign-img" src="{{authorized_signature}}" alt="">
      <img class="seal-img" src="{{company_seal}}" alt="">
    </div>
    <p class="signatory">{{authorized_signatory}}</p>
    <p class="signatory-role">{{authorized_signatory_designation}}</p>
  </div>

  <footer>
    This letter is issued by {{company_legal_name}} on request, without any liability.
    It can be verified with the HR department quoting Ref. No. {{letter_number}}.
  </footer>
</div>$html$ $f$;

CREATE OR REPLACE FUNCTION pg_temp.fnf_html() RETURNS text LANGUAGE sql IMMUTABLE AS $f$ SELECT $html$<div class="letter">
  <header class="letterhead">
    <img class="logo" src="{{company_logo}}" alt="">
    <div class="company">
      <div class="company-name">{{company_legal_name}}</div>
      <div class="company-line">{{company_address}}</div>
      <div class="company-line">{{company_registration}}</div>
    </div>
  </header>

  <div class="meta">
    <span>Ref. No.: <strong>{{letter_number}}</strong></span>
    <span>Date: <strong>{{issue_date_long}}</strong></span>
  </div>

  <h1>Full &amp; Final Settlement Statement</h1>

  <table class="facts">
    <tr><th>Employee</th><td>{{employee_salutation}} {{employee_name}} ({{employee_id}})</td>
        <th>Designation</th><td>{{designation}}</td></tr>
    <tr><th>Date of joining</th><td>{{date_of_joining_long}}</td>
        <th>Last working day</th><td>{{last_working_day_long}}</td></tr>
    <tr><th>Resignation date</th><td>{{resignation_date_long}}</td>
        <th>Service period</th><td>{{tenure}}</td></tr>
  </table>

  <div class="columns">
    <table class="lines">
      <thead><tr><th colspan="2">Earnings / Payables</th></tr></thead>
      <tbody>{{earnings_rows}}</tbody>
      <tfoot><tr><td>Total</td><td class="amt">{{total_earnings}}</td></tr></tfoot>
    </table>
    <table class="lines">
      <thead><tr><th colspan="2">Deductions / Recoveries</th></tr></thead>
      <tbody>{{deductions_rows}}</tbody>
      <tfoot><tr><td>Total</td><td class="amt">{{total_deductions}}</td></tr></tfoot>
    </table>
  </div>

  <div class="net">
    <div>{{net_amount_label}}: <strong>{{net_amount}}</strong></div>
    <div class="words">({{net_amount_words}})</div>
  </div>

  <table class="facts">
    <tr><th>Settlement status</th><td>{{settlement_status}}</td>
        <th>Approved by</th><td>{{settlement_approved_by}} ({{settlement_approved_date}})</td></tr>
    <tr><th>Payment date</th><td>{{payment_date}}</td>
        <th>Payment mode / reference</th><td>{{payment_mode}} {{payment_reference}}</td></tr>
  </table>
  <p>{{payment_details}}</p>
  <p>{{settlement_notes}}</p>

  <div class="signature">
    <p>For <strong>{{company_legal_name}}</strong></p>
    <div class="sign-area">
      <img class="sign-img" src="{{authorized_signature}}" alt="">
      <img class="seal-img" src="{{company_seal}}" alt="">
    </div>
    <p class="signatory">{{authorized_signatory}}</p>
    <p class="signatory-role">{{authorized_signatory_designation}}</p>
  </div>

  <footer>
    This statement is system generated by {{company_legal_name}}. Ref. No. {{letter_number}}.
  </footer>
</div>$html$ $f$;

CREATE OR REPLACE FUNCTION pg_temp.letter_css() RETURNS text LANGUAGE sql IMMUTABLE AS $f$ SELECT $css$@page { size: A4; margin: 16mm 18mm 18mm; }
body { font-family: "Segoe UI", Calibri, Arial, Helvetica, sans-serif; color: #1f2937; font-size: 11pt; line-height: 1.6; }
.letter { max-width: 175mm; margin: 0 auto; }
.letterhead { display: flex; align-items: center; gap: 16px; padding-bottom: 10px; border-bottom: 2px solid #1e3a8a; }
.logo { max-height: 56px; max-width: 150px; }
.company-name { font-size: 17pt; font-weight: 700; color: #1e3a8a; }
.company-line { font-size: 9pt; color: #6b7280; line-height: 1.4; }
.meta { display: flex; justify-content: space-between; margin: 16px 0 6px; font-size: 10.5pt; }
h1 { text-align: center; font-size: 14pt; text-transform: uppercase; letter-spacing: 1.5px; margin: 20px 0 16px; color: #111827; }
p { text-align: justify; margin: 0 0 12px; }
table { border-collapse: collapse; width: 100%; font-size: 10pt; }
.facts { margin: 0 0 14px; }
.facts th { text-align: left; color: #4b5563; font-weight: 600; padding: 4px 6px; width: 20%; }
.facts td { padding: 4px 6px; }
.columns { display: flex; gap: 14px; margin-bottom: 12px; }
.lines { border: 1px solid #e5e7eb; }
.lines th { background: #f3f4f6; text-align: left; padding: 6px; }
.lines td { padding: 5px 6px; border-top: 1px solid #f3f4f6; }
.lines td.amt, .lines td:last-child { text-align: right; }
.lines tfoot td { font-weight: 700; border-top: 1px solid #d1d5db; }
.none { color: #9ca3af; text-align: center !important; }
.net { border: 1px solid #1e3a8a; padding: 8px 10px; margin: 6px 0 14px; font-size: 11.5pt; }
.net .words { font-size: 9.5pt; color: #4b5563; }
.signature { margin-top: 28px; page-break-inside: avoid; }
.sign-area { display: flex; align-items: flex-end; gap: 40px; min-height: 56px; }
.sign-img { max-height: 56px; max-width: 180px; }
.seal-img { max-height: 70px; max-width: 90px; }
.signatory { font-weight: 700; margin: 6px 0 0; }
.signatory-role { margin: 0; color: #4b5563; }
footer { margin-top: 32px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 8pt; color: #9ca3af; text-align: center; }$css$ $f$;

DO $$
DECLARE
  s text;
  n int;
BEGIN
  FOR s IN SELECT * FROM pg_temp.hrms_schemas() LOOP
    IF to_regclass(format('%I.hcm_exit_letter_html_templates', s)) IS NULL
       OR to_regclass(format('%I.core_companies', s)) IS NULL THEN
      CONTINUE;
    END IF;
    EXECUTE format($q$
      INSERT INTO %1$I.hcm_exit_letter_html_templates
        (id, company_id, letter_type, name, html_body, css_styles, is_active, version, created_at, updated_at)
      SELECT gen_random_uuid(), c.id, t.letter_type, t.name, t.html, pg_temp.letter_css(), true, 1, now(), now()
      FROM %1$I.core_companies c
      CROSS JOIN (VALUES
        ('experience_relieving', 'Standard Experience & Relieving Letter', pg_temp.exr_html()),
        ('fnf_statement', 'Standard Full & Final Settlement Statement', pg_temp.fnf_html())
      ) AS t(letter_type, name, html)
      WHERE NOT EXISTS (
        SELECT 1 FROM %1$I.hcm_exit_letter_html_templates x
        WHERE x.company_id = c.id AND x.letter_type = t.letter_type)
    $q$, s);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '%: % default exit-letter template(s) added', s, n;
  END LOOP;
END $$;
