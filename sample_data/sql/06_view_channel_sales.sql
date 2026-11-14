-- 渠道销售视图：源表与 dwd 层 UNION ALL 对齐渠道口径
CREATE VIEW v_channel_sales AS
SELECT
  o.channel AS channel,
  CAST(o.amount AS DECIMAL(18,2)) AS amount
FROM ods_orders o
UNION ALL
SELECT
  d.channel AS channel,
  COALESCE(d.amount, 0) AS amount
FROM dwd_order d;
