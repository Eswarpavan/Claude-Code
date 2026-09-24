"""Database triggers that enforce hard rule 2 (no same-day round trips).

These run inside Postgres, so a bug in Python (or a manual SQL insert) cannot
create a sell that executes on or before the position's entry date.
"""

CREATE_SQL = """
CREATE OR REPLACE FUNCTION catalystedge_block_same_day_sell_order() RETURNS trigger AS $$
DECLARE entry date;
BEGIN
  IF NEW.side = 'sell' THEN
    SELECT entry_date INTO entry FROM paper_positions WHERE id = NEW.position_id;
    IF entry IS NULL THEN
      RAISE EXCEPTION 'sell order % has no position', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.execute_on <= entry THEN
      RAISE EXCEPTION 'rule 2: sell for position % scheduled on % but entered on %', NEW.position_id,
        NEW.execute_on, entry USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_no_same_day_sell
  BEFORE INSERT OR UPDATE OF execute_on, side, position_id ON paper_orders
  FOR EACH ROW EXECUTE FUNCTION catalystedge_block_same_day_sell_order();

CREATE OR REPLACE FUNCTION catalystedge_block_same_day_sell_fill() RETURNS trigger AS $$
DECLARE o_side text; entry date;
BEGIN
  SELECT o.side, p.entry_date INTO o_side, entry
    FROM paper_orders o LEFT JOIN paper_positions p ON p.id = o.position_id
   WHERE o.id = NEW.order_id;
  IF o_side = 'sell' AND (entry IS NULL OR NEW.fill_date <= entry) THEN
    RAISE EXCEPTION 'rule 2: sell fill on % for a position entered on %', NEW.fill_date, entry
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_fills_no_same_day_sell
  BEFORE INSERT OR UPDATE OF fill_date, order_id ON paper_fills
  FOR EACH ROW EXECUTE FUNCTION catalystedge_block_same_day_sell_fill();
"""

DROP_SQL = """
DROP TRIGGER IF EXISTS trg_fills_no_same_day_sell ON paper_fills;
DROP FUNCTION IF EXISTS catalystedge_block_same_day_sell_fill();
DROP TRIGGER IF EXISTS trg_orders_no_same_day_sell ON paper_orders;
DROP FUNCTION IF EXISTS catalystedge_block_same_day_sell_order();
"""
