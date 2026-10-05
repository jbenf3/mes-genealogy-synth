-- Q8 device history: every state of the lot q8_lot_name, with the event that produced it. The event columns are null for
-- a legacy state, which no event in the data produced.
WITH states AS (
    SELECT out_lot_uuid AS lot_uuid, out_lot_sequence AS sequence FROM events WHERE out_lot_name = getvariable('q8_lot_name')
    UNION
    SELECT in_lot_uuid, in_lot_sequence FROM events WHERE in_lot_name = getvariable('q8_lot_name')
),
produced AS (
    SELECT DISTINCT out_lot_uuid, out_material, segment_name, equipment_path, location, begin_ts FROM events
    WHERE out_lot_name = getvariable('q8_lot_name')
)
SELECT s.lot_uuid, s.sequence, p.out_material AS material, p.segment_name, p.equipment_path, p.location, p.begin_ts
FROM states s LEFT JOIN produced p ON p.out_lot_uuid = s.lot_uuid
ORDER BY s.sequence, p.begin_ts NULLS FIRST, s.lot_uuid;
