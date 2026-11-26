"""Standard layouts + base styles for the Payslip, Experience & Relieving
Letter and F&F Settlement Statement Designs galleries (generated from the
company standard templates; see template_designs_more.py for the themes)."""

PAYSLIP_LAYOUT = r"""<div class="payslip">
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
</div>"""

PAYSLIP_CSS = r""".payslip {
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
}"""

EXPERIENCE_LAYOUT = r"""<div class="letter">
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
      <div class="sign-space"></div>
      <div class="seal">Company Seal</div>
    </div>
    <p class="signatory">{{authorized_signatory}}</p>
    <p class="signatory-role">{{authorized_signatory_designation}}</p>
  </div>

  <footer>
    This letter is issued by {{company_legal_name}} on request, without any liability.
    It can be verified with the HR department quoting Ref. No. {{letter_number}}.
  </footer>
</div>"""

EXPERIENCE_CSS = r"""@page { size: A4; margin: 16mm 18mm 18mm; }
body { font-family: "Segoe UI", Calibri, Arial, Helvetica, sans-serif; color: #1f2937; font-size: 11.5pt; line-height: 1.65; }
.letter { max-width: 175mm; margin: 0 auto; }
.letterhead { display: flex; align-items: center; gap: 16px; padding-bottom: 10px; border-bottom: 2px solid #1e3a8a; }
.logo { max-height: 56px; max-width: 150px; }
.logo[src=""] { display: none; }
.company-name { font-size: 17pt; font-weight: 700; color: #1e3a8a; letter-spacing: 0.2px; }
.company-line { font-size: 9pt; color: #6b7280; line-height: 1.4; }
.meta { display: flex; justify-content: space-between; margin: 16px 0 6px; font-size: 10.5pt; }
h1 { text-align: center; font-size: 14pt; text-transform: uppercase; letter-spacing: 1.5px; margin: 20px 0 2px; color: #111827; }
.addressee { text-align: center; font-weight: 600; font-size: 10.5pt; letter-spacing: 0.5px; margin: 0 0 20px; }
p { text-align: justify; margin: 0 0 12px; }
.signature { margin-top: 30px; page-break-inside: avoid; }
.sign-area { display: flex; align-items: flex-end; gap: 40px; height: 64px; }
.sign-space { width: 200px; }
.seal { width: 86px; height: 56px; border: 1px dashed #cbd5e1; border-radius: 6px; color: #cbd5e1; font-size: 8pt; display: flex; align-items: center; justify-content: center; }
.signatory { font-weight: 700; margin: 6px 0 0; }
.signatory-role { margin: 0; color: #4b5563; }
footer { margin-top: 36px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 8pt; color: #9ca3af; text-align: center; line-height: 1.4; }"""

FNF_LAYOUT = r"""<div class="letter">
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
</div>"""

FNF_CSS = r"""@page { size: A4; margin: 16mm 18mm 18mm; }
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
footer { margin-top: 32px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 8pt; color: #9ca3af; text-align: center; }"""
