# Apple 商品目录维护说明

## 文件
- `apple_cn_devices.json`

## 维护原则
- 价格使用 `Apple 中国官网`公开起售价。
- 如果一个价格对应的是“系列起售价”，请在 `price_note` 写清楚。
- 售后条款使用 Apple 官方条款，避免写平台或商家自定义承诺。
- 每次更新后把 `meta.verified_on` 改为当天日期（`YYYY-MM-DD`）。

## 建议更新步骤
1. 打开 `https://www.apple.com.cn/shop/buy-iphone`、`/buy-ipad`、`/buy-mac`、`/buy-watch` 校对起售价。
2. 打开对应 `support.apple.com/zh-cn/...` 技术规格页校对芯片、容量、尺寸等关键字段。
3. 更新 `apple_cn_devices.json` 后，保存即可生效（服务会按文件修改时间自动重载）。
4. 通过 `GET /healthz` 检查：
   - `product_catalog_loaded=true`
   - `product_catalog_products` 数量正确
   - `product_catalog_verified_on` 日期正确

## 说明
- 聊天推荐逻辑读取此 JSON，不需要改 Python 代码。
- 客服回复中仍会提示“实际成交价以下单页为准”。
