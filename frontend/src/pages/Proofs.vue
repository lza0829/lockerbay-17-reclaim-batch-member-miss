<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const proofs = ref([])
const runs = ref([])
const selectedRunId = ref(null)
const attest = ref(null)

async function loadAttest() {
  attest.value = selectedRunId.value ? await api.reclaimAttest(selectedRunId.value) : null
}
async function selectRun(runId) {
  selectedRunId.value = runId
  await loadAttest()
}
onMounted(async () => {
  proofs.value = await api.proofs()
  runs.value = await api.reclaimRuns()
  if (runs.value.length) {
    selectedRunId.value = runs.value[0].run_id
    await loadAttest()
  }
})
</script>
<template>
  <div data-plant="17-reclaim-batch-member-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>核账</h1>
    <p class="sub">占用核对与回收趟次成员核证：成员投影与占用账实关集合全等方为 valid。</p>

    <div class="panel" style="overflow:auto">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收趟次成员核证（只读）</h2>
      <p v-if="!runs.length" class="sub">暂无回收趟次。</p>
      <template v-else>
        <p class="sub">
          选择趟次：
          <select :value="selectedRunId" @change="selectRun($event.target.value)"
                  style="background:#111827;color:#e8eef6;border:1px solid #334155;border-radius:4px;padding:.3rem">
            <option v-for="r in runs" :key="r.run_id" :value="r.run_id">{{ r.run_id }}（closed_n={{ r.closed_n }}）</option>
          </select>
        </p>
        <table v-if="attest" style="margin-bottom:.75rem">
          <tbody>
            <tr><td style="width:12rem">趟次 run_id</td><td><code>{{ attest.run_id }}</code></td></tr>
            <tr><td>扫描/关账时刻</td><td>{{ attest.scanned_at }}</td></tr>
            <tr><td>closed_n / 成员数 / 实关数</td><td>{{ attest.closed_n }} / {{ attest.member_count }} / {{ attest.closed_count }}</td></tr>
            <tr><td>核证结论</td>
              <td><b :style="attest.valid ? 'color:#34d399' : 'color:#f87171'">{{ attest.valid ? 'valid：投影与实关集合全等' : 'INVALID：投影分叉' }}</b></td>
            </tr>
          </tbody>
        </table>
        <table v-if="attest">
          <thead><tr><th>硬约束</th><th>判定</th></tr></thead>
          <tbody>
            <tr v-for="(ok, key) in attest.checks" :key="key">
              <td><code>{{ key }}</code></td>
              <td :style="ok ? 'color:#34d399' : 'color:#f87171'">{{ ok ? '✓ 通过' : '✗ 违例' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="attest && !attest.valid" class="sub" style="color:#f87171;font-size:.82rem;margin-top:.5rem">
          缺行 {{ attest.missing_member_ids.join(',') || '—' }} ·
          越界 {{ attest.extra_member_ids.join(',') || '—' }} ·
          含未关闭 {{ attest.unclosed_member_ids.join(',') || '—' }} ·
          关闭时刻不符 {{ attest.closed_at_mismatch_ids.join(',') || '—' }}
        </p>
      </template>
    </div>

    <div class="panel" style="overflow:auto;margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用核证</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>开启</th><th>事件</th></tr></thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.id">
            <td><code>{{ p.occupancy_id }}</code></td>
            <td>{{ p.cell_id }}</td>
            <td>{{ p.tracking_no }}</td>
            <td>{{ p.opened_at }}</td>
            <td><code>{{ p.event_ids_json }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
