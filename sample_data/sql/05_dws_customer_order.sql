-- 客户 × 订单汇总底表：只取已支付订单
CREATE TABLE dws_customer_order AS
SELECT
  c.customer_id,
  c.customer_name,
  o.order_id,
  o.amount,
  o.status
FROM dwd_customer c
JOIN dwd_order o ON c.customer_id = o.customer_id
WHERE o.status = 'paid';
