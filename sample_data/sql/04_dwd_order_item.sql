-- 订单行落 dwd：先用子查询过滤无效行，再算行金额
CREATE TABLE dwd_order_item AS
SELECT
  t.item_id,
  t.order_id,
  t.sku,
  t.qty,
  ROUND(t.qty * t.unit_price, 2) AS amount
FROM (
  SELECT item_id, order_id, sku, qty, unit_price
  FROM ods_order_items
  WHERE qty > 0
) t;
