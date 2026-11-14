-- 故意埋的环：v_loop_a 引用 v_loop_b
CREATE VIEW v_loop_a AS
SELECT
  b.order_id AS order_id,
  b.amount AS amount
FROM v_loop_b b;
