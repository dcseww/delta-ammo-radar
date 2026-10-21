# 分析：一天中哪个时间段所有弹药价格总体最便宜
#
# 数据基础：price_agg_1h（每小时均价聚合，含回补的 7 天历史 + 实时采集）
# 方法：
#   1. 对每个物品，用其所有小时均价的总均值作为基准价
#   2. 计算每个物品在各小时的"相对价格"= 该小时均价 / 基准价
#      （归一化后不同口径、不同档位的弹药才可横向比较）
#   3. 按小时跨物品聚合相对价格 → 全市场"便宜指数"（1.00 = 平均水平，越低越便宜）
#   4. 同时统计每个小时是多少物品的"全天最低价时段"
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DB = r"D:\seek\子弹\data\ammo.sqlite3"
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

con = sqlite3.connect(DB)
cur = con.cursor()

# ---- 0. 数据概览 ----
print("== price_kind 分布 ==")
for row in cur.execute("SELECT price_kind, COUNT(*) FROM price_agg_1h GROUP BY price_kind"):
    print(" ", row)

r = cur.execute("SELECT MIN(bucket_ts), MAX(bucket_ts), COUNT(DISTINCT ammo_id) FROM price_agg_1h").fetchone()
t0, t1, n_items = r
print(f"\n== 时间范围 ==\n  {datetime.fromtimestamp(t0, TZ):%Y-%m-%d %H:%M} ~ {datetime.fromtimestamp(t1, TZ):%Y-%m-%d %H:%M}"
      f"  ({(t1 - t0) / 86400:.1f} 天), {n_items} 个物品")

# ---- 1. 取每小时均价（unit_price 口径优先）----
rows = cur.execute("""
    SELECT ammo_id, bucket_ts, avg, samples
    FROM price_agg_1h
    WHERE price_kind = 'unit'
""").fetchall()
if not rows:
    rows = cur.execute("SELECT ammo_id, bucket_ts, avg, samples FROM price_agg_1h").fetchall()

# 每个物品：小时 -> 价格累加
item_hour_sum = defaultdict(lambda: defaultdict(float))  # ammo -> hour -> sum(avg*samples)
item_hour_cnt = defaultdict(lambda: defaultdict(float))  # ammo -> hour -> sum(samples)
item_total_sum = 0.0
item_total_cnt = defaultdict(float)

for ammo_id, ts, avg, samples in rows:
    h = datetime.fromtimestamp(ts, TZ).hour
    w = samples or 1
    item_hour_sum[ammo_id][h] += (avg or 0) * w
    item_hour_cnt[ammo_id][h] += w
    item_total_sum += 0  # 占位
    item_total_cnt[ammo_id] += w

# ---- 2. 相对价格指数 ----
# hour -> [各物品的相对价格]
hour_rel = defaultdict(list)
hour_min_items = defaultdict(int)   # hour -> 多少物品在该小时达到全天最低
per_item_hours = {}

for ammo_id, hours in item_hour_sum.items():
    # 该物品整体均价（按样本加权）
    base_num = sum(hours.values())
    base_den = item_total_cnt[ammo_id]
    if base_den == 0:
        continue
    base = base_num / base_den
    if base <= 0:
        continue
    rels = {}
    for h in range(24):
        if h in hours and item_hour_cnt[ammo_id][h] > 0:
            rel = (hours[h] / item_hour_cnt[ammo_id][h]) / base
            rels[h] = rel
            hour_rel[h].append(rel)
    if rels:
        min_h = min(rels, key=rels.get)
        hour_min_items[min_h] += 1
        per_item_hours[ammo_id] = rels

n_used_items = len(per_item_hours)
print(f"\n参与分析的物品：{n_used_items} 个")

# ---- 3. 输出 24 小时表 ----
print("\n" + "=" * 78)
print(f"{'时段':<6}{'便宜指数':>10}{'相对均价偏差':>14}{'最低价物品数':>14}{'样本物品数':>12}")
print("-" * 78)
results = []
for h in range(24):
    rels = hour_rel.get(h, [])
    if not rels:
        results.append((h, None, 0, 0))
        continue
    m = sum(rels) / len(rels)
    results.append((h, m, hour_min_items.get(h, 0), len(rels)))
    dev = (m - 1) * 100
    print(f"{h:02d}:00  {m:>10.4f}  {dev:>+12.2f}%  {hour_min_items.get(h, 0):>12d}  {len(rels):>10d}")

# ---- 4. 结论 ----
valid = [(h, m) for h, m, _, _ in results if m is not None]
rank = sorted(valid, key=lambda x: x[1])
print("\n== 最便宜的 5 个小时 ==")
for h, m in rank[:5]:
    print(f"  {h:02d}:00  指数 {m:.4f}  （比全天平均低 {(1 - m) * 100:.2f}%）")
print("\n== 最贵的 5 个小时 ==")
for h, m in rank[-5:][::-1]:
    print(f"  {h:02d}:00  指数 {m:.4f}  （比全天平均高 {(m - 1) * 100:.2f}%）")

cheapest, c_val = rank[0]
dearest, d_val = rank[-1]
print(f"\n== 结论 ==")
print(f"  最低价时段 {cheapest:02d}:00，最高价时段 {dearest:02d}:00，")
print(f"  在最低时段买入平均可省 {(d_val - c_val) / d_val * 100:.2f}%（相对最贵时段）。")

# 数据可信度提示：跨度天数
days = (t1 - t0) / 86400
print(f"\n（注意：数据仅覆盖 {days:.1f} 天，每小时的样本来自约 {days:.0f} 天的均值，")
print("  短期波动可能影响结论；天数越多该规律越可靠。）")
