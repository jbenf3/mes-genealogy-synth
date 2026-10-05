-- Q6 processed on: every lot consumed or produced by an event on equipment q6_equipment with
-- q6_from <= begin_ts < q6_to.
WITH hits AS (
    SELECT in_lot_name, out_lot_name FROM events
    WHERE equipment_path = getvariable('q6_equipment') AND begin_ts >= getvariable('q6_from') AND begin_ts < getvariable('q6_to')
)
SELECT in_lot_name AS lot_name FROM hits WHERE in_lot_name IS NOT NULL
UNION
SELECT out_lot_name FROM hits WHERE out_lot_name IS NOT NULL
ORDER BY lot_name;
