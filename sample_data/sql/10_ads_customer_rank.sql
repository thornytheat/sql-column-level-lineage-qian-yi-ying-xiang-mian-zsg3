-- 客户消费排行：两层嵌套子查询，先聚合再补客户名称
CREATE TABLE ads_customer_rank AS
SELECT
  t.customer_id AS customer_id,
  t.customer_name AS customer_name,
  COALESCE(t.gmv, 0) AS gmv,
  t.order_cnt AS order_cnt
FROM (
  SELECT
    c.customer_id,
    c.customer_name,
    SUM(o.amount) AS gmv,
    COUNT(o.order_id) AS order_cnt
  FROM (
    SELECT customer_id, customer_name FROM v_customer
  ) c
  JOIN dws_customer_order o ON c.customer_id = o.customer_id
  GROUP BY c.customer_id, c.customer_name
) t;
