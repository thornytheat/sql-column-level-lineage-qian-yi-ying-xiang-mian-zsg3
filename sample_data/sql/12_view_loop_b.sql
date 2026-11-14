-- 故意埋的环：v_loop_b 又引用 v_loop_a，与 11 构成表级环（列级同样成环）
CREATE VIEW v_loop_b AS
SELECT
  a.order_id AS order_id,
  a.amount AS amount
FROM v_loop_a a;
