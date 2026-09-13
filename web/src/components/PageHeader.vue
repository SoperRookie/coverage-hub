<script setup lang="ts">
// 页头：面包屑 + 标题 + 元信息 + 右侧动作。所有页面共用，保证层级感一致。
defineProps<{ title: string; crumbs?: { label: string; to?: string }[] }>();
</script>

<template>
  <div class="page-header">
    <div v-if="crumbs && crumbs.length" class="crumbs">
      <template v-for="(c, i) in crumbs" :key="i">
        <router-link v-if="c.to" :to="c.to">{{ c.label }}</router-link>
        <template v-else>{{ c.label }}</template>
        <span v-if="i < crumbs.length - 1">/</span>
      </template>
    </div>
    <div class="row">
      <div>
        <h1>{{ title }}</h1>
        <div class="meta"><slot name="meta" /></div>
      </div>
      <div class="actions"><slot name="actions" /></div>
    </div>
  </div>
</template>
