// wrangler は *.bin を Data モジュール（ArrayBuffer）として読み込む
declare module "*.bin" {
  const data: ArrayBuffer;
  export default data;
}
