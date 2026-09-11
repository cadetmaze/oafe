-- Curation tier: what may appear in the default "Popular" browse grid.
--
-- CONFIRMED LIVE (user-reported: "anyone searches for any xyz thing and it
-- comes to popular section... only the best quality dataset should come, not
-- any random xyz unknown cheap dataset"). Root cause: /feed ordered by
-- `created_at desc`, so ANY dataset a user's search happened to auto-discover
-- became the newest rows in the corpus and instantly dominated Popular.
--
-- Popularity alone is NOT a sufficient filter here: markov-ai/computer-use,
-- the flagship trajectory dataset, has 0 downloads and 0 likes on the Hub,
-- while plenty of genuinely uninteresting sets have thousands. So tier is a
-- union of "hand-verified/featured" and "demonstrably reputable".
--   2 = featured  : explicitly curated, appears in a top-nav collection
--   1 = reputable : real Hub traction (downloads >= 1000 or likes >= 10)
--   0 = unvetted  : auto-discovered; searchable, but never in Popular
alter table hf_datasets add column if not exists curation_tier smallint not null default 0;
create index if not exists hf_datasets_curation_tier_idx on hf_datasets (curation_tier desc);

update hf_datasets
   set curation_tier = 1
 where curation_tier < 1
   and (coalesce(downloads, 0) >= 1000 or coalesce(likes, 0) >= 10);
