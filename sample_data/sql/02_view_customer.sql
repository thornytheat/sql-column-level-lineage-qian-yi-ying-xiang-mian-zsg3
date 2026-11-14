-- 客户视图：直接透传 dwd 层四列，下游报表按此视图取数
CREATE VIEW v_customer AS
SELECT
  customer_id,
  customer_name,
  city,
  level
FROM dwd_customer;
