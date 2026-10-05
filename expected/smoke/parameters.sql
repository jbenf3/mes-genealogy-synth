-- Parameters of the reference queries for the smoke scenario. The expected answers are the .csv files in this folder.
-- They hold for the data whose manifest.json has the content_sha256 in content_sha256.txt.

-- Q1: the last state of unit CL000000000066. Its ancestry has a re-minted state and a legacy lot.
SET VARIABLE q1_lot_uuid = '386949fdaaf779f92d1af96441632d37';
-- Q2: a roll, just before it is cut into units.
SET VARIABLE q2_lot_uuid = '3520e196bbe116fc086f9e5a4fa48c77';
-- Q3: the test step where the planted failures show, from the day the first units that passed the planted cause reach it
-- to the end of the data.
SET VARIABLE q3_segment = 'SEG_041';
SET VARIABLE q3_from = TIMESTAMP '2000-01-12 00:00:00';
SET VARIABLE q3_to = TIMESTAMP '2000-01-15 00:00:00';
-- Q4: a unit whose components include the feeder it was dispensed from.
SET VARIABLE q4_lot_name = 'CL000000000101';
-- Q5: a feeder. Every part dispensed from any of its states counts.
SET VARIABLE q5_lot_name = 'CT0000000000';
-- Q6: a dispensing machine, for one hour.
SET VARIABLE q6_equipment = 'AR03/EQ_099';
SET VARIABLE q6_from = TIMESTAMP '2000-01-10 00:00:00';
SET VARIABLE q6_to = TIMESTAMP '2000-01-10 01:00:00';
-- Q7: the refill station, for the hour of a refill. Only the parts dispensed after the refill are affected.
SET VARIABLE q7_equipment = 'AR08/EQ_165';
SET VARIABLE q7_from = TIMESTAMP '2000-01-11 00:00:00';
SET VARIABLE q7_to = TIMESTAMP '2000-01-11 01:00:00';
-- Q8: a unit with a re-minted sequence.
SET VARIABLE q8_lot_name = 'CL000000000066';
