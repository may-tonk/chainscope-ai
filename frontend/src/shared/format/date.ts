/**
 * 日期、时间的显示格式。全站统一用这里的函数，不在各页面各写一份。
 *
 * 后端返回的时间都是 ISO 8601 字符串（带时区，例如 "2026-10-26T08:07:50Z"），
 * 这里按浏览器所在时区换算后，用中文格式显示。
 */

/** 把 ISO 时间字符串格式化成“2026年10月26日”这样的中文日期；格式意外时原样返回，不显示 Invalid Date。 */
export function formatDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "long" }).format(date);
}
