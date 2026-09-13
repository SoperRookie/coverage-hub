import { nextTick } from "vue";
import { theme, type ThemeChoice } from "./theme";

// 把页面内容区导出成 PDF 下载到本地：整块内容先画成一张高清位图（html2canvas），再按 A4 横向
// 切页塞进 jsPDF。走位图而不是文字的原因：hub 常在内网，浏览器里现成的中文字体就能画出来，
// 不用往包里塞几 MB 的 CJK 字体；图表（canvas）也原样进去。代价是 PDF 里的文字不能选中。
// 两个库都是按需动态 import 的：只有点了导出才加载那 ~700 KB。
export async function exportPdf(el: HTMLElement, filename: string) {
  const [{ default: html2canvas }, { jsPDF }] = await Promise.all([import("html2canvas"), import("jspdf")]);

  // 导出一律用浅色：深色底打印出来一页黑；图表随主题重画，等它们画完
  const prev: ThemeChoice = theme.value;
  const switched = theme.value !== "light";
  if (switched) {
    theme.value = "light";
    await nextTick();
    await new Promise((r) => setTimeout(r, 350));
  }
  el.classList.add("exporting");
  try {
    const canvas = await html2canvas(el, {
      scale: 2, useCORS: false, logging: false,
      backgroundColor: "#ffffff",
      ignoreElements: (node) => node.classList?.contains("no-print") ?? false,
    });
    const pdf = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4", compress: true });
    const pageW = pdf.internal.pageSize.getWidth();
    const pageH = pdf.internal.pageSize.getHeight();
    const margin = 8;
    const imgW = pageW - margin * 2;
    const pxPerMm = canvas.width / imgW;
    const sliceH = Math.floor((pageH - margin * 2) * pxPerMm);   // 每页能放的像素高度

    for (let y = 0, page = 0; y < canvas.height; y += sliceH, page++) {
      const h = Math.min(sliceH, canvas.height - y);
      const slice = document.createElement("canvas");
      slice.width = canvas.width;
      slice.height = h;
      const ctx = slice.getContext("2d")!;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, slice.width, slice.height);
      ctx.drawImage(canvas, 0, y, canvas.width, h, 0, 0, canvas.width, h);
      if (page > 0) pdf.addPage();
      pdf.addImage(slice.toDataURL("image/jpeg", 0.92), "JPEG", margin, margin, imgW, h / pxPerMm);
      pdf.setFontSize(8);
      pdf.setTextColor(140);
      pdf.text(`covhub · ${page + 1}`, pageW - margin, pageH - 3, { align: "right" });
    }
    pdf.save(filename);
  } finally {
    el.classList.remove("exporting");
    if (switched) theme.value = prev;
  }
}
