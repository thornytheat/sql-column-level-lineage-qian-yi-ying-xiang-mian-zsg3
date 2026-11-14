-- 商品视图：SELECT * 全列透传（依赖商品源表 schema 展开）
CREATE VIEW v_product_sales AS
SELECT * FROM ods_products;
