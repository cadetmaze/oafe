-- ============================================================================
-- Round 4 fixes: dataset "kind" classification (sub-filters) + cleanup helpers
-- ============================================================================

alter table hf_datasets add column if not exists dataset_kind text; -- 'captioned' | 'labeled' | 'detection_segmentation' | 'other'
alter table assets add column if not exists dataset_kind text;

create index if not exists hf_datasets_kind_idx on hf_datasets(dataset_kind);
create index if not exists assets_kind_idx on assets(dataset_kind);

-- Backfill from existing detected caption/label state (best-effort guess for
-- data ingested before this migration; a real crawl_dataset() re-run will
-- overwrite this with the properly-classified value).
update assets set dataset_kind = case
  when caption is not null and caption <> '' and length(caption) > 3 then 'captioned'
  when labels is not null and array_length(labels, 1) > 0 then 'labeled'
  else 'other'
end
where dataset_kind is null;

update hf_datasets h set dataset_kind = (
  select a.dataset_kind from assets a where a.source_dataset = h.repo_id limit 1
) where dataset_kind is null;
