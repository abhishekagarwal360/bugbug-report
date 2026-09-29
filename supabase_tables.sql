-- AA-bugbug cache, in the seo-digest Supabase project (uvdeabpuozztqliatehe).
-- Closed nights are stored once, so later runs only ask BugBug for the latest night.

create table if not exists public.bugbug_test_runs (
  id          uuid primary key,          -- BugBug test-run id (what JIRA links point to)
  project     text not null,             -- 91mobiles | MSP | 91m Indonesia
  test_id     uuid not null,
  name        text,
  status      text not null,             -- passed | failed | error | skipped | stopped ...
  error_code  text,
  started     timestamptz,
  night       date,                      -- 3 PM IST -> 3 PM IST window, labelled by first date
  web_url     text,
  synced_at   timestamptz not null default now()
);
create index if not exists bugbug_test_runs_project_night on public.bugbug_test_runs (project, night);
create index if not exists bugbug_test_runs_test on public.bugbug_test_runs (test_id);

-- One row per project per closed night = "this night is fully cached, don't re-fetch".
create table if not exists public.bugbug_nights (
  project     text not null,
  night       date not null,
  runs        int  not null,             -- BugBug's total_count for the window (completeness check)
  tests       int,                       -- unique test cases run
  failed      int,                       -- test cases whose last run failed
  synced_at   timestamptz not null default now(),
  primary key (project, night)
);

-- Only the service key (used by the skill) may read/write.
alter table public.bugbug_test_runs enable row level security;
alter table public.bugbug_nights   enable row level security;

-- BugBug-related JIRA tickets (link in description, bugbug label/component, or "bugbug" in summary).
create table if not exists public.bugbug_jira (
  key             text primary key,        -- M91-13016
  jira_project    text not null,           -- M91 | MSP
  bugbug_project  text,                    -- 91mobiles | MSP | 91m Indonesia (from the link, else inferred)
  summary         text,
  issue_type      text,
  status          text,
  created         date not null,
  has_link        boolean not null,        -- true = description carries an app.bugbug.io link
  matched_by      text,                    -- link | label | summary | component
  url             text,
  synced_at       timestamptz not null default now()
);
create index if not exists bugbug_jira_created on public.bugbug_jira (created desc);
alter table public.bugbug_jira enable row level security;

-- added 25-Sep-2026
alter table public.bugbug_jira   add column if not exists reporter text;
alter table public.bugbug_jira   add column if not exists bugbug_url text;   -- the app.bugbug.io link in the description
-- a night still running is cached as partial=true and re-synced once it closes
alter table public.bugbug_nights add column if not exists partial boolean not null default false;
