-- 订单落 dwd：金额 CAST 到 decimal(18,2)，空渠道补 unknown
INSERT INTO dwd_order (order_id, customer_id, amount, status, channel, created_at)
SELECT
  o.order_id,
  o.customer_id,
  CAST(o.amount AS DECIMAL(18,2)) AS amount,
  o.status,
  COALESCE(o.channel, 'unknown') AS channel,
  o.created_at
FROM ods_orders o;
