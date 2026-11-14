-- 类目销售报表：子查询里先拼商品类目，再按类目聚合
CREATE TABLE ads_category_report AS
SELECT
  s.category AS category,
  COUNT(s.sku) AS sku_cnt,
  ROUND(SUM(s.amount), 2) AS total_amount,
  ROUND(AVG(s.amount), 2) AS avg_amount
FROM (
  SELECT i.item_id, i.sku, i.amount, p.category
  FROM dwd_order_item i
  JOIN dim_product p ON i.sku = p.sku
) s
GROUP BY s.category;
