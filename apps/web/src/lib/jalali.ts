/** ابزار تقویم شمسی برای نمای ماهانه وظایف. */

import { jalaaliMonthLength, toGregorian, toJalaali } from "jalaali-js";

export const J_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];
export const J_WEEKDAYS = ["ش", "ی", "د", "س", "چ", "پ", "ج"];

export type JMonth = { jy: number; jm: number };

export function currentJMonth(): JMonth {
  const t = toJalaali(new Date());
  return { jy: t.jy, jm: t.jm };
}

export function shiftMonth(m: JMonth, delta: number): JMonth {
  let jm = m.jm + delta;
  let jy = m.jy;
  while (jm > 12) {
    jm -= 12;
    jy++;
  }
  while (jm < 1) {
    jm += 12;
    jy--;
  }
  return { jy, jm };
}

export function jToDate(jy: number, jm: number, jd: number): Date {
  const g = toGregorian(jy, jm, jd);
  return new Date(g.gy, g.gm - 1, g.gd);
}

/** خانه‌های تقویم ماه (شروع هفته از شنبه) */
export function monthGrid(m: JMonth): { date: Date | null; jd: number | null }[] {
  const first = jToDate(m.jy, m.jm, 1);
  const offset = (first.getDay() + 1) % 7; // شنبه = ۰
  const len = jalaaliMonthLength(m.jy, m.jm);
  const cells: { date: Date | null; jd: number | null }[] = [];
  for (let i = 0; i < offset; i++) cells.push({ date: null, jd: null });
  for (let d = 1; d <= len; d++) cells.push({ date: jToDate(m.jy, m.jm, d), jd: d });
  while (cells.length % 7) cells.push({ date: null, jd: null });
  return cells;
}

export const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
