<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const rows = ref([])
const q = ref([])
const reclaims = ref([])
const report = ref(null)
const members = ref({ closed_n: 0, members: [] })
const busy = ref(false)
async function load() {
  rows.value = await api.occupancies()
  q.value = await api.quarantine()
  reclaims.value = await api.reclaims()
  members.value = await api.reclaimMembers()
}
async function run() {
  busy.value = true
  try {
    report.value = await api.runReclaim()
    await load()
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <div data-plant="17-reclaim-batch-member-miss" class="plant-mark" style="display:none"></div>

  <div>
    <h1>回收</h1>
    <p class="sub">按已落地 reclaim_deadline_at 裁决：到期关占用、格口回 free、留核对；未到期不动。扫描与关账装载后提交。</p>
    <p>
      <button :disabled="busy" @click="run">跑一轮超时回收扫描</button>
      <span v-if="report" class="sub" style="margin-left:.75rem">
        扫描时刻 <code>{{ report.scanned_at }}</code> · 本次关账 <b>{{ report.reclaimed_count }}</b> 条 · 补写 deadline {{ report.backfilled_deadlines }} 条
      </span>
    </p>
    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">趟次成员</h2>
      <p class="sub">closed_n={{ members.closed_n }} · 成员 {{ (members.members||[]).length }}</p>
    </div>
    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收核对（只读）</h2>
      <p v-if="!reclaims.length" class="sub">暂无超时回收记录。</p>
      <table v-else>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>回收截止</th><th>关闭时刻</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="r in reclaims" :key="r.occupancy_id">
            <td><code>{{ r.occupancy_id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>{{ r.reclaim_deadline_at }}</td>
            <td>{{ r.closed_at }}</td>
            <td>{{ r.closed_reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">占用账</h2>
      <table>
        <thead><tr><th>占用</th><th>格口</th><th>运单</th><th>状态</th><th>开启</th><th>回收截止</th><th>关闭</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td><code>{{ r.id }}</code></td>
            <td>{{ r.cell_id }}</td>
            <td>{{ r.tracking_no }}</td>
            <td>{{ r.status }}<template v-if="r.closed_reason">（{{ r.closed_reason }}）</template></td>
            <td>{{ r.opened_at }}</td>
            <td>{{ r.reclaim_deadline_at || '—' }}</td>
            <td>{{ r.closed_at || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">搁置</h2>
      <p v-if="!q.length" class="sub">暂无搁置。</p>
      <table v-else>
        <thead><tr><th>id</th><th>原因</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="item in q" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
