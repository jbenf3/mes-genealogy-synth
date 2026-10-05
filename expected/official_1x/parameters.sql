-- Parameters of the reference queries for the official_1x scenario. The expected answers are the .csv files in this folder.
-- They hold for the data whose manifest.json has the content_sha256 in content_sha256.txt.

-- Q1: the last state of unit CL000000057663. Its ancestry has a re-minted state.
SET VARIABLE q1_lot_uuid = '047541883d91004374d22e0c066c7902';
-- Q2: a roll, just before it is cut into units.
SET VARIABLE q2_lot_uuid = '001a0bfc2dc0cc0b5e5d6557fa566955';
-- Q3: the test step where the planted failures show, for one day in the update stream when both planted causes are active.
SET VARIABLE q3_segment = 'SEG_041';
SET VARIABLE q3_from = TIMESTAMP '2000-08-10 00:00:00';
SET VARIABLE q3_to = TIMESTAMP '2000-08-11 00:00:00';
-- Q4: a unit whose components include the feeder it was dispensed from.
SET VARIABLE q4_lot_name = 'CL000000012358';
-- Q5: a feeder. Every part dispensed from any of its states counts.
SET VARIABLE q5_lot_name = 'CT0000000001';
-- Q6: a merge machine, for one hour in the update stream.
SET VARIABLE q6_equipment = 'AR07/EQ_102';
SET VARIABLE q6_from = TIMESTAMP '2000-09-04 13:00:00';
SET VARIABLE q6_to = TIMESTAMP '2000-09-04 14:00:00';
-- Q7: a refill station, for the hour of a refill in the update stream. Only the parts dispensed after the refill are
-- affected.
SET VARIABLE q7_equipment = 'AR01/EQ_165';
SET VARIABLE q7_from = TIMESTAMP '2000-08-03 06:00:00';
SET VARIABLE q7_to = TIMESTAMP '2000-08-03 07:00:00';
-- Q8: a unit with a re-minted sequence.
SET VARIABLE q8_lot_name = 'CL000000000224';
