-- Raw website capture for SQL-side ICP. No HTML. Applied to whatever
-- project SUPABASE_URL on the MCP service points at (do not hardcode).

CREATE TABLE IF NOT EXISTS public.site_pages (
    id bigserial PRIMARY KEY,
    domain text NOT NULL,
    url text NOT NULL,
    page_type text,
    http_status int,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    title text,
    meta_description text,
    h1 text[],
    body_text text,
    links jsonb,
    emails text[],
    phones text[],
    error text,
    source_table text,
    source_id text,
    UNIQUE (domain, url)
);

CREATE INDEX IF NOT EXISTS site_pages_domain_idx ON public.site_pages (domain);
CREATE INDEX IF NOT EXISTS site_pages_source_idx ON public.site_pages (source_table, source_id);

CREATE INDEX IF NOT EXISTS site_pages_fts_idx ON public.site_pages
    USING gin (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' || coalesce(meta_description, '') || ' ' || coalesce(body_text, '')
        )
    );

ALTER TABLE public.site_pages ENABLE ROW LEVEL SECURITY;

GRANT ALL ON TABLE public.site_pages TO service_role;
GRANT ALL ON SEQUENCE public.site_pages_id_seq TO service_role;

NOTIFY pgrst, 'reload schema';
