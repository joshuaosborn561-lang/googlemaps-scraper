-- Complete public.pp_* helpers expected by gmscraper/source_binding.py.
-- Signatures must stay in lockstep with the Python rpc() payloads:
--   pp_count_rows(p_schema text, p_table text, p_where text) -> bigint
--   pp_ensure_columns(p_schema text, p_table text, p_columns jsonb) -> integer
--   pp_select_rows(p_schema, p_table, p_columns jsonb, p_where, p_order_by,
--                  p_limit int, p_offset int) -> setof jsonb
--   pp_patch_row(p_schema, p_table, p_key_column, p_key_value text,
--                p_patch jsonb) -> boolean
--
-- resolve_places writeback calls pp_patch_row (not pp_update_rows).

CREATE OR REPLACE FUNCTION public.pp_count_rows(
  p_schema text,
  p_table text,
  p_where text DEFAULT NULL
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  n bigint;
  q text;
BEGIN
  IF p_schema IS NULL OR p_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$'
     OR p_table IS NULL OR p_table !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
    RAISE EXCEPTION 'invalid schema/table';
  END IF;
  IF p_schema NOT IN ('public', 'client_peterson', 'client_basco', 'permit_parcel', 'gc', 'lp')
     AND p_schema !~ '^client_[a-z][a-z0-9_]*$' THEN
    RAISE EXCEPTION 'schema not allowed: %', p_schema;
  END IF;
  q := format('select count(*) from %I.%I', p_schema, p_table);
  IF coalesce(p_where, '') <> '' THEN
    q := q || ' where ' || p_where;
  END IF;
  EXECUTE q INTO n;
  RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION public.pp_ensure_columns(
  p_schema text,
  p_table text,
  p_columns jsonb
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  k text;
  v text;
  v_norm text;
  n int := 0;
BEGIN
  IF p_schema IS NULL OR p_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$'
     OR p_table IS NULL OR p_table !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
    RAISE EXCEPTION 'invalid schema/table';
  END IF;
  IF p_schema NOT IN ('public', 'client_peterson', 'client_basco', 'permit_parcel', 'gc', 'lp')
     AND p_schema !~ '^client_[a-z][a-z0-9_]*$' THEN
    RAISE EXCEPTION 'schema not allowed: %', p_schema;
  END IF;
  IF p_columns IS NULL OR jsonb_typeof(p_columns) <> 'object' THEN
    RETURN 0;
  END IF;
  FOR k, v IN SELECT key, value #>> '{}' FROM jsonb_each(p_columns)
  LOOP
    IF k !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
      RAISE EXCEPTION 'invalid column name: %', k;
    END IF;
    v_norm := lower(btrim(coalesce(v, '')));
    IF v_norm NOT IN (
      'text', 'boolean', 'bool', 'integer', 'int', 'int4', 'bigint', 'int8',
      'numeric', 'double precision', 'float8', 'real',
      'timestamptz', 'timestamp with time zone', 'jsonb', 'date'
    ) THEN
      RAISE EXCEPTION 'type not allowed: %', v;
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = p_schema AND table_name = p_table AND column_name = k
    ) THEN
      EXECUTE format('alter table %I.%I add column %I %s', p_schema, p_table, k, v_norm);
      n := n + 1;
    END IF;
  END LOOP;
  RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION public.pp_select_rows(
  p_schema text,
  p_table text,
  p_columns jsonb,
  p_where text DEFAULT NULL,
  p_order_by text DEFAULT NULL,
  p_limit integer DEFAULT 1000,
  p_offset integer DEFAULT 0
)
RETURNS SETOF jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  q text;
  cols text;
  order_sql text := '';
  ident text;
  dir text := 'ASC';
  lim int;
  off int;
BEGIN
  IF p_schema IS NULL OR p_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$'
     OR p_table IS NULL OR p_table !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
    RAISE EXCEPTION 'invalid schema/table';
  END IF;
  IF p_schema NOT IN ('public', 'client_peterson', 'client_basco', 'permit_parcel', 'gc', 'lp')
     AND p_schema !~ '^client_[a-z][a-z0-9_]*$' THEN
    RAISE EXCEPTION 'schema not allowed: %', p_schema;
  END IF;

  IF p_columns IS NOT NULL AND jsonb_typeof(p_columns) = 'array' THEN
    SELECT string_agg(format('%I', c), ', ')
      INTO cols
      FROM jsonb_array_elements_text(p_columns) AS c
     WHERE c ~ '^[A-Za-z_][A-Za-z0-9_]*$';
  END IF;
  IF cols IS NULL OR cols = '' THEN
    cols := '*';
  END IF;

  IF coalesce(p_order_by, '') <> '' THEN
    ident := btrim(split_part(p_order_by, ' ', 1));
    IF ident !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
      RAISE EXCEPTION 'invalid order_by: %', p_order_by;
    END IF;
    IF upper(btrim(split_part(p_order_by, ' ', 2))) = 'DESC' THEN
      dir := 'DESC';
    END IF;
    order_sql := format(' order by %I %s', ident, dir);
  END IF;

  lim := GREATEST(1, LEAST(coalesce(p_limit, 1000), 100000));
  off := GREATEST(0, coalesce(p_offset, 0));

  q := format(
    'select to_jsonb(t) from (select %s from %I.%I',
    cols, p_schema, p_table
  );
  IF coalesce(p_where, '') <> '' THEN
    q := q || ' where ' || p_where;
  END IF;
  q := q || order_sql || format(' limit %s offset %s) t', lim, off);
  RETURN QUERY EXECUTE q;
END;
$$;

CREATE OR REPLACE FUNCTION public.pp_patch_row(
  p_schema text,
  p_table text,
  p_key_column text,
  p_key_value text,
  p_patch jsonb
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  k text;
  v jsonb;
  sets text;
  piece text;
  n int := 0;
  q text;
BEGIN
  IF p_schema IS NULL OR p_schema !~ '^[A-Za-z_][A-Za-z0-9_]*$'
     OR p_table IS NULL OR p_table !~ '^[A-Za-z_][A-Za-z0-9_]*$'
     OR p_key_column IS NULL OR p_key_column !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
    RAISE EXCEPTION 'invalid schema/table/key_column';
  END IF;
  IF p_schema NOT IN ('public', 'client_peterson', 'client_basco', 'permit_parcel', 'gc', 'lp')
     AND p_schema !~ '^client_[a-z][a-z0-9_]*$' THEN
    RAISE EXCEPTION 'schema not allowed: %', p_schema;
  END IF;
  IF p_patch IS NULL OR jsonb_typeof(p_patch) <> 'object' THEN
    RETURN false;
  END IF;

  sets := NULL;
  FOR k, v IN SELECT key, value FROM jsonb_each(p_patch)
  LOOP
    IF k !~ '^[A-Za-z_][A-Za-z0-9_]*$' THEN
      RAISE EXCEPTION 'invalid patch column: %', k;
    END IF;
    piece := CASE jsonb_typeof(v)
      WHEN 'null' THEN format('%I = NULL', k)
      WHEN 'boolean' THEN format('%I = %s::boolean', k, v #>> '{}')
      WHEN 'number' THEN format('%I = %s::numeric', k, v #>> '{}')
      WHEN 'object' THEN format('%I = %L::jsonb', k, v::text)
      WHEN 'array' THEN format('%I = %L::jsonb', k, v::text)
      ELSE format('%I = %L', k, v #>> '{}')
    END;
    sets := CASE WHEN sets IS NULL THEN piece ELSE sets || ', ' || piece END;
  END LOOP;
  IF sets IS NULL THEN
    RETURN false;
  END IF;

  q := format(
    'update %I.%I set %s where %I::text = %L',
    p_schema, p_table, sets, p_key_column, coalesce(p_key_value, '')
  );
  EXECUTE q;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n > 0;
END;
$$;

COMMENT ON FUNCTION public.pp_count_rows(text, text, text) IS
  'source_binding.count_pending / validate_binding';
COMMENT ON FUNCTION public.pp_ensure_columns(text, text, jsonb) IS
  'source_binding.ensure_writeback_columns; p_columns is {name: pg_type}';
COMMENT ON FUNCTION public.pp_select_rows(text, text, jsonb, text, text, integer, integer) IS
  'source_binding.fetch_pending; p_columns is a JSON array of identifiers';
COMMENT ON FUNCTION public.pp_patch_row(text, text, text, text, jsonb) IS
  'source_binding.patch_row — one-row writeback for resolve_places';

REVOKE ALL ON FUNCTION public.pp_count_rows(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pp_ensure_columns(text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pp_select_rows(text, text, jsonb, text, text, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pp_patch_row(text, text, text, text, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.pp_count_rows(text, text, text) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.pp_ensure_columns(text, text, jsonb) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.pp_select_rows(text, text, jsonb, text, text, integer, integer) TO anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.pp_patch_row(text, text, text, text, jsonb) TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
