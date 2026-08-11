-- ════════════════════════════════════════════════════════════════════
-- 家庭自驾记账 · Supabase 数据库结构
-- 用法：登录 Supabase 控制台 → 左侧 SQL Editor → 新建查询 →
--       把本文件全部内容粘贴进去 → 点「Run」执行一次即可（只需一次）。
-- ════════════════════════════════════════════════════════════════════

-- 1) 账本表
create table if not exists households (
  id              uuid primary key default gen_random_uuid(),
  invite_code     text unique not null,
  name            text,
  members         uuid[] not null default '{}',
  member_profiles jsonb not null default '{}',
  owner           uuid,
  created_at      timestamptz default now()
);

-- 2) 账单明细表
create table if not exists transactions (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  amount       numeric not null,
  category     text,
  note         text,
  paid_by      text,
  paid_by_id   text,
  date         text,
  created_by   text,
  is_deleted   boolean default false,
  created_at   timestamptz default now()
);

create index if not exists idx_tx_hid on transactions(household_id);

-- 3) 开启行级安全（RLS）
alter table households   enable row level security;
alter table transactions enable row level security;

-- 账本：登录用户可读（用于按邀请码查找）；创建者/成员可改
drop policy if exists hh_select on households;
create policy hh_select on households for select to authenticated using (true);
drop policy if exists hh_insert on households;
create policy hh_insert on households for insert to authenticated with check (true);
drop policy if exists hh_update on households;
create policy hh_update on households for update to authenticated
  using (auth.uid() = owner or auth.uid() = any(coalesce(members, '{}')));
drop policy if exists hh_delete on households;
create policy hh_delete on households for delete to authenticated
  using (auth.uid() = owner);

-- 账单：仅账本成员可读写
drop policy if exists tx_all on transactions;
create policy tx_all on transactions for all to authenticated
  using (auth.uid() = any(select members from households where id = household_id))
  with check (auth.uid() = any(select members from households where id = household_id));

-- 4) 用邀请码加入账本的存储过程（SECURITY DEFINER，绕过 RLS 把当前用户加进成员）
create or replace function join_household(p_code text, p_name text)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  hid uuid;
  mem uuid[];
  prof jsonb;
begin
  select id, members, member_profiles into hid, mem, prof
    from households where invite_code = p_code;
  if hid is null then
    raise exception 'INVALID_CODE';
  end if;
  if not (auth.uid() = any(coalesce(mem, '{}'))) then
    mem := mem || auth.uid();
  end if;
  prof := jsonb_set(prof, array[auth.uid()::text], to_jsonb(p_name));
  update households set members = mem, member_profiles = prof where id = hid;
  return hid;
end;
$$;

grant execute on function join_household(text, text) to authenticated;
