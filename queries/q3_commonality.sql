-- Q3 commonality: which equipment do the units that failed the test step have in common?
-- Members: the units that went through step q3_segment with q3_from <= begin_ts < q3_to. A member failed if the event
-- right after it scrapped the unit (status ST_2). For each equipment, count the failing and the passing members that have
-- it in their history: the events that produced the member or any of its ancestors.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, out_lot_uuid FROM events WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
tested AS (
    SELECT DISTINCT out_lot_uuid AS lot_uuid FROM events
    WHERE segment_name = getvariable('q3_segment') AND begin_ts >= getvariable('q3_from') AND begin_ts < getvariable('q3_to')
),
members AS (
    SELECT t.lot_uuid, EXISTS (SELECT 1 FROM events s WHERE s.in_lot_uuid = t.lot_uuid AND s.out_lot_status = 'ST_2') AS failed
    FROM tested t
),
history(member, lot_uuid) AS (
    SELECT lot_uuid, lot_uuid FROM members
    UNION
    SELECT h.member, e.in_lot_uuid FROM history h JOIN edges e ON e.out_lot_uuid = h.lot_uuid
),
equipment AS (
    SELECT DISTINCT h.member, p.equipment_path FROM history h JOIN events p ON p.out_lot_uuid = h.lot_uuid
    WHERE p.equipment_path IS NOT NULL
)
SELECT q.equipment_path, count(*) FILTER (WHERE m.failed) AS failing, count(*) FILTER (WHERE NOT m.failed) AS passing
FROM equipment q JOIN members m ON m.lot_uuid = q.member
GROUP BY q.equipment_path
ORDER BY q.equipment_path;
