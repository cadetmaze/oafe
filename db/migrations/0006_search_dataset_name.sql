-- Index the DATASET NAME into the search vector.
--
-- CONFIRMED LIVE: searching "cifar" matched 0 of the 870 CIFAR assets in the
-- corpus, because the only text indexed for such a row is its caption —
-- literally the word "frog". Same root cause made "aircraft" return Monet
-- paintings (incidental caption matches) instead of the three real aircraft
-- datasets, and ranked jxie/coco_captions above markov-ai/computer-use for
-- the query "computer use".
--
-- `source_dataset` is a slug like "uoft-cs/cifar10" or
-- "markov-ai/computer-use-large". to_tsvector on the raw slug tokenises it as
-- one blob, so we split on / - _ . and digits-vs-letters boundaries first
-- ("cifar10" -> "cifar 10") to make each part independently searchable.
--
-- Weight B: a dataset-name hit is strong evidence — stronger than an
-- incidental caption word (D) — but must not outrank a true caption match (A),
-- otherwise every asset of a big dataset would flood a one-word query.

create or replace function dataset_name_text(repo text) returns text as $$
  select regexp_replace(
           regexp_replace(coalesce(repo, ''), '([a-zA-Z])([0-9])', '\1 \2', 'g'),
           '[/_.\-]+', ' ', 'g');
$$ language sql immutable;

create or replace function assets_search_trigger() returns trigger as $$
begin
  new.search_vector :=
    setweight(to_tsvector('english', coalesce(new.caption, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(array_to_string(new.labels, ' '), '')), 'B') ||
    setweight(to_tsvector('english', dataset_name_text(new.source_dataset)), 'B') ||
    setweight(to_tsvector('english', coalesce(array_to_string(new.tags, ' '), '')), 'C') ||
    setweight(to_tsvector('english', coalesce(new.alt_text, '') || ' ' || coalesce(new.description, '')), 'D');
  return new;
end
$$ language plpgsql;
