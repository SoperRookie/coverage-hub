<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import "element-plus/es/components/message/style/css";
import { api, type Project } from "../api";

// 新建 / 编辑项目。编辑时 name 不可改：它是 URL 段和服务的归属键。
const props = defineProps<{ modelValue: boolean; editing: Project | null; onError: (err: unknown) => boolean }>();
const emit = defineEmits<{ (e: "update:modelValue", v: boolean): void; (e: "saved", name: string): void }>();

const form = reactive({ name: "", title: "", description: "" });
const saving = ref(false);
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

watch(() => props.modelValue, (open) => {
  if (!open) return;
  form.name = props.editing?.name ?? "";
  form.title = props.editing?.title ?? "";
  form.description = props.editing?.description ?? "";
});

async function save() {
  if (!props.editing && !NAME_RE.test(form.name)) {
    ElMessage.warning("项目名只能用字母、数字、. _ -，且不能以 . 或 - 开头");
    return;
  }
  saving.value = true;
  try {
    const body = { title: form.title || null, description: form.description || null };
    if (props.editing) {
      await api.updateProject(props.editing.name, body);
    } else {
      await api.createProject({ name: form.name, ...body });
    }
    ElMessage.success(props.editing ? "已保存" : `已创建项目 ${form.name}`);
    emit("update:modelValue", false);
    emit("saved", props.editing ? props.editing.name : form.name);
  } catch (err) {
    if (!props.onError(err)) ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="editing ? `编辑项目 ${editing.name}` : '新建项目'" width="480px"
             @update:model-value="emit('update:modelValue', $event)">
    <el-form label-width="72px" @submit.prevent="save">
      <el-form-item label="标识" required>
        <el-input v-model="form.name" :disabled="!!editing" placeholder="如 order-domain（字母、数字、. _ -）" />
      </el-form-item>
      <el-form-item label="名称">
        <el-input v-model="form.title" placeholder="看板上显示的名字，可选" />
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" type="textarea" :rows="2" placeholder="可选" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!editing && !form.name" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>
