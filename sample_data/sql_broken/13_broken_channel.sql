-- 故意写错的脚本：ROUND 的括号没有闭合，用于演示解析诊断定位
CREATE VIEW v_bad_channel AS
SELECT
  o.channel AS channel,
  ROUND(o.amount, 2 AS amount
FROM ods_orders o;
