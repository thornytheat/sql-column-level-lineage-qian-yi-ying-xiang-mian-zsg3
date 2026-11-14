-- 客户主数据清洗：去空格、城市统一大写，落到 dwd 层
CREATE TABLE dwd_customer AS
SELECT
  c.customer_id,
  TRIM(c.customer_name) AS customer_name,
  UPPER(TRIM(c.city)) AS city,
  c.level
FROM ods_customers c;
