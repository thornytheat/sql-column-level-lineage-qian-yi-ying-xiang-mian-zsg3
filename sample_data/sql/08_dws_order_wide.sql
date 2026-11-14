-- 订单宽表：o.* 展开 dwd_order 全部列，再补客户名称与城市
CREATE TABLE dws_order_wide AS
SELECT
  o.*,
  c.customer_name AS customer_name,
  c.city AS city
FROM dwd_order o
JOIN dim_customer c ON o.customer_id = c.customer_id;
