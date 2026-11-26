-- Expense claims post to IMPACGO Finance (app/finance_link.py). ADDITIVE and safe to run again; applied to every schema that has HRMS's tables (each tenant and _template).
--
--   core_employees.payable_account_id        the employee's payable GL account in Finance (what the company owes them); an approved claim credits it
--   hcm_expense_claim_types.gl_account_id    the expense GL account in Finance for the category (Flight, Hotel, Taxi ...); several categories may share one
--   hcm_expense_claims.finance_*             the claim's journal in Finance (DRAFT while pending, POSTED once approved), its payment's journal, the last error, and for a
--                                            claim in another currency the rate IMPACGO Finance gave, the base currency and the base amount (HRMS keeps no FX rates)
--   hcm_asset_inventory.finance_asset_id     the fixed asset in Finance's register this HRMS asset is (one HRMS asset per Finance asset); HRMS reports who holds it
--   hcm_asset_inventory.finance_custodian_asset_id / finance_error   the Finance asset whose holder HRMS last reported, and Finance's last refusal
--   hcm_finance_link_settings                per company: its Finance address, the HRMS connector key (encrypted, TENANT_EMAIL_ENCRYPTION_KEY) and the accounts
--                                            (Administration > Integrations > IMPACGO Finance)
--   hcm_finance_outbox                       documents waiting to be brought in line with Finance (queued in the same transaction as the change)
--
--   psql -d <db> -v ON_ERROR_STOP=1 -f db/2026_11_17_finance_expense_claims.sql
BEGIN;
DO $$
DECLARE s text;
BEGIN
  FOR s IN SELECT table_schema FROM information_schema.tables WHERE table_name = 'hcm_expense_claims' AND table_type = 'BASE TABLE'
           AND EXISTS (SELECT 1 FROM information_schema.tables t2 WHERE t2.table_schema = information_schema.tables.table_schema AND t2.table_name = 'core_employees') LOOP
    EXECUTE format('ALTER TABLE %I.core_employees ADD COLUMN IF NOT EXISTS payable_account_id uuid', s);
    EXECUTE format('ALTER TABLE %I.hcm_expense_claim_types ADD COLUMN IF NOT EXISTS gl_account_id uuid', s);
    EXECUTE format('ALTER TABLE %I.hcm_expense_claims ADD COLUMN IF NOT EXISTS finance_tx_id uuid, ADD COLUMN IF NOT EXISTS finance_status varchar(12), '
                   'ADD COLUMN IF NOT EXISTS finance_voucher_no varchar(40), ADD COLUMN IF NOT EXISTS finance_payment_tx_id uuid, '
                   'ADD COLUMN IF NOT EXISTS finance_payment_voucher_no varchar(40), ADD COLUMN IF NOT EXISTS finance_error text, '
                   'ADD COLUMN IF NOT EXISTS finance_exchange_rate numeric(18,8), ADD COLUMN IF NOT EXISTS finance_base_currency varchar(3), '
                   'ADD COLUMN IF NOT EXISTS finance_base_amount numeric(14,2)', s);
    IF to_regclass(format('%I.hcm_asset_inventory', s)) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE %I.hcm_asset_inventory ADD COLUMN IF NOT EXISTS finance_asset_id uuid, ADD COLUMN IF NOT EXISTS finance_custodian_asset_id uuid, '
                     'ADD COLUMN IF NOT EXISTS finance_error text', s);
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_hcm_asset_inventory_finance_asset ON %I.hcm_asset_inventory (company_id, finance_asset_id) WHERE finance_asset_id IS NOT NULL', s);
    END IF;
    EXECUTE format($t$
      CREATE TABLE IF NOT EXISTS %1$I.hcm_finance_link_settings (
          company_id uuid PRIMARY KEY,
          enabled boolean NOT NULL DEFAULT true,
          finance_url varchar(300) NOT NULL,
          finance_tenant varchar(63),
          api_key_encrypted text,                   -- Fernet ciphertext ('enc:v1:...'); never plaintext
          key_hint varchar(8),
          journal_name varchar(50),
          bank_account_id uuid, advance_account_id uuid, claim_tax_account_id uuid, business_unit_id uuid,
          finance_company_name varchar(200),
          finance_base_currency varchar(3),
          verified_at timestamptz,
          updated_by uuid,
          updated_at timestamptz)$t$, s);
    EXECUTE format('ALTER TABLE %I.hcm_finance_link_settings ADD COLUMN IF NOT EXISTS finance_base_currency varchar(3)', s);
    EXECUTE format($t$
      CREATE TABLE IF NOT EXISTS %1$I.hcm_finance_outbox (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          company_id uuid NOT NULL,
          doctype varchar(30) NOT NULL,             -- expense_claim | asset_custodian (doc = HRMS asset) | asset_custodian_clear (doc = Finance asset)
          doc_id uuid NOT NULL,
          status varchar(12) NOT NULL DEFAULT 'pending',
          attempts integer NOT NULL DEFAULT 0,
          last_error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_hcm_finance_outbox_status CHECK (status IN ('pending', 'done', 'failed')))$t$, s);
    -- one waiting row per document: a later change of the same document joins it
    EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS ux_hcm_finance_outbox_waiting ON %I.hcm_finance_outbox (company_id, doctype, doc_id) WHERE status = ''pending''', s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_hcm_finance_outbox_doc ON %I.hcm_finance_outbox (company_id, doc_id)', s);
  END LOOP;
END $$;
COMMIT;
