-- PostgREST ON CONFLICT (domain, email) requires a non-partial UNIQUE constraint.
-- The old partial index (WHERE email IS NOT NULL AND email <> '') is not enough
-- and caused 42P10, discarding paid enrichment after vendors returned hits.
--
-- basco_contacts already has basco_contacts_domain_email_key (added manually);
-- this migration is idempotent for all per-client contacts tables.

-- public.{slug}_contacts (PostgREST-visible)
CREATE UNIQUE INDEX IF NOT EXISTS basco_contacts_domain_email_key
    ON public.basco_contacts (domain, email);
CREATE UNIQUE INDEX IF NOT EXISTS peterson_contacts_domain_email_key
    ON public.peterson_contacts (domain, email);

-- Legacy client_* schemas (if still present)
CREATE UNIQUE INDEX IF NOT EXISTS client_basco_contacts_domain_email_key
    ON client_basco.contacts (domain, email);
CREATE UNIQUE INDEX IF NOT EXISTS client_peterson_contacts_domain_email_key
    ON client_peterson.contacts (domain, email);

-- Shared gc.contacts used when client_tag is omitted
CREATE UNIQUE INDEX IF NOT EXISTS gc_contacts_domain_email_key
    ON gc.contacts (domain, email);
