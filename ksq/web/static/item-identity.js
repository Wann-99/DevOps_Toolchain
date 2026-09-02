/* 下单商品标识解析：item_id 取值优先级统一为 商品编码(out_item_id) > SKU ID(sku_id) > 69码(sku_code)。 */
(function (global) {
  const ITEM_ID_CANDIDATE_FIELDS = ["out_item_id", "sku_id", "sku_code"];

  function pickItemId(source) {
    if (!source) return "";
    for (let index = 0; index < ITEM_ID_CANDIDATE_FIELDS.length; index += 1) {
      const value = String(source[ITEM_ID_CANDIDATE_FIELDS[index]] || "").trim();
      if (value) return value;
    }
    return "";
  }

  global.KsqItemIdentity = { pickItemId: pickItemId };
})(window);
