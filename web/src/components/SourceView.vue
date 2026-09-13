<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, type SourceView } from "../api";

// 一个文件的新增代码：新增行按覆盖状态标色，前后带上下文。
// 这里的红绿是「这一行执行过没有」的事实（JaCoCo 自己的报告也这么标），不是按阈值给覆盖率上色。
const props = defineProps<{ service: string; kind: "runtime" | "unit"; file: string; version?: string | null; onError: (err: unknown) => boolean }>();
const view = ref<SourceView | null>(null);
const error = ref("");

onMounted(async () => {
  try {
    view.value = await api.source(props.service, props.kind, props.file, props.version);
  } catch (err) {
    if (!props.onError(err)) error.value = err instanceof Error ? err.message : String(err);
  }
});
</script>

<template>
  <div v-if="error" class="muted">{{ error }}</div>
  <div v-else-if="!view" class="muted">加载源码…</div>
  <div v-else class="src">
    <div class="src-head">
      <span class="mono">{{ view.reportFile }}</span>
      <span class="muted">新增 {{ view.added }} 行，可覆盖 {{ view.total }} 行，覆盖 {{ view.covered }} 行</span>
      <span class="legend"><i class="sw covered"></i>执行过</span>
      <span class="legend"><i class="sw missed"></i>未执行</span>
      <span class="legend"><i class="sw nocode"></i>无探针（空行 / 注释 / 声明）</span>
      <span class="spacer"></span>
      <a v-if="view.reportUrl" class="plain" :href="view.reportUrl" target="_blank">在 JaCoCo 报告里看整个文件</a>
    </div>
    <div v-if="!view.sourceFound" class="muted" style="margin: 6px 0">
      hub 上找不到这个文件的源码（服务没配 <code>sourcefiles</code>，或这份归档早于 hub 开始保存源码片段），只列行号：
    </div>
    <table class="code">
      <tbody>
        <tr v-for="(l, i) in view.lines" :key="i" :class="l.status">
          <td class="nr">{{ l.nr ?? "" }}</td>
          <td class="mark">{{ l.status === "covered" ? "+" : l.status === "missed" ? "+" : l.status === "nocode" ? "+" : "" }}</td>
          <td class="txt"><pre>{{ l.text ?? "" }}</pre></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.src { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; margin: 4px 0 8px; }
.src-head { display: flex; align-items: center; gap: 12px; padding: 6px 10px; background: #fafafa; border-bottom: 1px solid var(--line); font-size: 12px; }
.src-head .spacer { flex: 1; }
.sw { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.sw.covered { background: #c8e6c9; }
.sw.missed { background: #ffcdd2; }
.sw.nocode { background: #eeeeee; }
table.code { border-collapse: collapse; width: 100%; font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 12px; }
table.code td { padding: 0 8px; vertical-align: top; white-space: pre; }
table.code td.nr { width: 48px; text-align: right; color: #9aa0a6; user-select: none; border-right: 1px solid var(--line); }
table.code td.mark { width: 12px; color: #9aa0a6; user-select: none; }
table.code pre { margin: 0; font: inherit; }
tr.covered { background: #e8f5e9; }
tr.missed { background: #ffebee; }
tr.nocode { background: #f5f5f5; }
tr.gap td { color: #9aa0a6; text-align: center; background: #fafafa; }
</style>
