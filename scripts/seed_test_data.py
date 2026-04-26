"""
seed_test_data.py — 灌入 SEMI schema 測試資料

執行方式:
  cd H:\copilotCli
  python scripts/seed_test_data.py

連線設定沿用 Oracle MCP 的 .env
"""

import oracledb
import random
from datetime import datetime, timedelta

# ── 連線設定（與 oracle-mcp-server .env 一致）──────────
CONN_STR = "system/Oracle123@localhost:1521/FREEPDB1"
SCHEMA = "SEMI"

# ── 參數 ─────────────────────────────────────────────
FABS = ["FAB_A", "FAB_B", "FAB_C"]
PRODUCTS = ["PROD_X100", "PROD_Y200", "PROD_Z300"]
PROCESS_NODES = ["7nm", "5nm", "3nm"]
WAT_PARAMS = [
    ("VTH_N",   "V",    0.25,  0.35,  0.30),
    ("VTH_P",   "V",   -0.40, -0.28, -0.34),
    ("IDSAT_N", "uA",  450.0, 650.0, 550.0),
    ("IDSAT_P", "uA",  200.0, 380.0, 290.0),
    ("BV",      "V",    5.0,  12.0,   8.0),
    ("RSHEET",  "ohm",  80.0, 150.0, 110.0),
    ("TOX",     "A",    18.0,  24.0,  21.0),
    ("VTLIN_N", "V",    0.30,  0.45,  0.38),
]

random.seed(42)

def rand_date(base: datetime, days_back: int) -> datetime:
    return base - timedelta(days=random.randint(0, days_back),
                            hours=random.randint(0, 23),
                            minutes=random.randint(0, 59))


def main():
    conn = oracledb.connect(CONN_STR)
    cur = conn.cursor()
    print("Connected to Oracle")

    now = datetime(2026, 4, 15)  # 資料基準日

    # ═══════════════ CP 資料 ═══════════════
    cp_lots = []
    lot_seq = 0
    for fab in FABS:
        for prod in PRODUCTS:
            for _ in range(random.randint(3, 6)):
                lot_seq += 1
                fab_code = fab[-1]  # A, B, C
                lot_id = f"CP2026{fab_code}{lot_seq:03d}"
                wafer_count = random.randint(10, 25)
                node = random.choice(PROCESS_NODES)
                start = rand_date(now, 90)
                end = start + timedelta(hours=random.randint(8, 72))
                cp_lots.append((lot_id, prod, fab, wafer_count, node, start, end))

    # CP_LOT
    print(f"[CP_LOT] Inserting {len(cp_lots)} lots...")
    cur.executemany(
        f"INSERT INTO {SCHEMA}.CP_LOT (LOT_ID, PRODUCT_ID, FAB_ID, WAFER_COUNT, PROCESS_NODE, START_TIME, END_TIME, VALID_FLAG, CREATE_TIME, UPDATE_TIME) "
        f"VALUES (:1, :2, :3, :4, :5, :6, :7, 1, SYSDATE, SYSDATE)",
        cp_lots
    )

    # CP_WAFER + CP_BIN_SUMMARY
    wafer_rows = []
    bin_rows = []
    for lot_id, prod, fab, wc, node, start, end in cp_lots:
        for w in range(1, wc + 1):
            wafer_id = f"{w:02d}"
            total_die = random.randint(800, 1500)
            # 良率在 0.75 ~ 0.99 之間，偶爾有低良率
            base_yield = random.uniform(0.82, 0.98)
            if random.random() < 0.08:
                base_yield = random.uniform(0.50, 0.80)  # 低良率 wafer
            pass_die = int(total_die * base_yield)
            fail_die = total_die - pass_die
            yld = round(pass_die / total_die, 4)
            test_time = start + timedelta(hours=random.randint(1, int((end - start).total_seconds() / 3600) or 1))
            wafer_rows.append((lot_id, wafer_id, total_die, pass_die, fail_die, yld, test_time))

            # Bin 分佈: Bin 1 = pass, Bin 2-5 = fail categories
            bin_rows.append((lot_id, wafer_id, 1, pass_die))
            remaining = fail_die
            for bin_no in [2, 3, 4, 5]:
                if remaining <= 0:
                    break
                if bin_no == 5:
                    cnt = remaining
                else:
                    cnt = random.randint(0, remaining)
                bin_rows.append((lot_id, wafer_id, bin_no, cnt))
                remaining -= cnt

    print(f"[CP_WAFER] Inserting {len(wafer_rows)} wafers...")
    cur.executemany(
        f"INSERT INTO {SCHEMA}.CP_WAFER (LOT_ID, WAFER_ID, TOTAL_DIE, PASS_DIE, FAIL_DIE, YIELD, TEST_TIME, VALID_FLAG) "
        f"VALUES (:1, :2, :3, :4, :5, :6, :7, 1)",
        wafer_rows
    )

    print(f"[CP_BIN_SUMMARY] Inserting {len(bin_rows)} bin records...")
    cur.executemany(
        f"INSERT INTO {SCHEMA}.CP_BIN_SUMMARY (LOT_ID, WAFER_ID, BIN_NO, DIE_COUNT) "
        f"VALUES (:1, :2, :3, :4)",
        bin_rows
    )

    # ═══════════════ WAT 資料 ═══════════════
    # WAT_PARAM
    print(f"[WAT_PARAM] Inserting {len(WAT_PARAMS)} params...")
    param_rows = []
    for pid, unit, lsl, usl, target in WAT_PARAMS:
        param_rows.append((pid, pid, unit, usl, lsl, target))
    cur.executemany(
        f"INSERT INTO {SCHEMA}.WAT_PARAM (PARAM_ID, PARAM_NAME, UNIT, USL, LSL, TARGET, VALID_FLAG) "
        f"VALUES (:1, :2, :3, :4, :5, :6, 1)",
        param_rows
    )

    # WAT_LOT
    wat_lots = []
    wat_seq = 0
    engineers = ["ENG_CHEN", "ENG_LIN", "ENG_WANG", "ENG_LEE"]
    for fab in ["FAB_A", "FAB_B"]:
        for prod in PRODUCTS:
            for _ in range(random.randint(3, 5)):
                wat_seq += 1
                fab_code = fab[-1]
                lot_id = f"WAT2026{fab_code}{wat_seq:03d}"
                test_date = rand_date(now, 90)
                eng = random.choice(engineers)
                wat_lots.append((lot_id, prod, fab, test_date, eng))

    print(f"[WAT_LOT] Inserting {len(wat_lots)} lots...")
    cur.executemany(
        f"INSERT INTO {SCHEMA}.WAT_LOT (LOT_ID, PRODUCT_ID, FAB_ID, TEST_DATE, ENG_ID, VALID_FLAG, CREATE_TIME) "
        f"VALUES (:1, :2, :3, :4, :5, 1, SYSDATE)",
        wat_lots
    )

    # WAT_RESULT
    result_rows = []
    for lot_id, prod, fab, test_date, eng in wat_lots:
        wafer_count = random.randint(3, 8)
        for w in range(1, wafer_count + 1):
            wafer_id = f"{w:02d}"
            for site in random.sample(range(1, 10), random.randint(3, 5)):
                for pid, unit, lsl, usl, target in WAT_PARAMS:
                    # 正常值在 target ± 範圍內，偶爾超規
                    spread = (usl - lsl) * 0.3
                    val = random.gauss(target, spread)
                    if random.random() < 0.04:
                        # 故意超規
                        val = usl + abs(random.gauss(0, spread)) if random.random() > 0.5 else lsl - abs(random.gauss(0, spread))
                    val = round(val, 6)
                    pf = "P" if lsl <= val <= usl else "F"
                    result_rows.append((lot_id, wafer_id, site, pid, val, pf))

    print(f"[WAT_RESULT] Inserting {len(result_rows)} measurements...")
    # 分批 insert（量可能很大）
    batch_size = 5000
    for i in range(0, len(result_rows), batch_size):
        batch = result_rows[i:i + batch_size]
        cur.executemany(
            f"INSERT INTO {SCHEMA}.WAT_RESULT (LOT_ID, WAFER_ID, SITE_NO, PARAM_ID, MEAS_VALUE, PASS_FAIL, CREATE_TIME) "
            f"VALUES (:1, :2, :3, :4, :5, :6, SYSDATE)",
            batch
        )
        print(f"  batch {i // batch_size + 1}: {len(batch)} rows")

    conn.commit()
    print("\n✅ All test data committed!")

    # 驗證
    for tbl in ["CP_LOT", "CP_WAFER", "CP_BIN_SUMMARY", "WAT_LOT", "WAT_PARAM", "WAT_RESULT"]:
        cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{tbl}")
        cnt = cur.fetchone()[0]
        print(f"  {tbl}: {cnt} rows")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
