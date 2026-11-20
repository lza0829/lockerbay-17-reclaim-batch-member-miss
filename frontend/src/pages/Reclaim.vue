<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const rows = ref([])
const q = ref([])
const reclaims = ref([])
const runs = ref([])
const report = ref(null)
const members = ref({ closed_n: 0, member_count: 0, members: [] })
const attest = ref(null)
const selectedRunId = ref(null)
const busy = ref(false)

async function load() {
  rows.value = await api.occupancies()
  q.value = await api.quarantine()
  reclaims.value = await api.reclaims()
  runs.value = await api.reclaimRuns()
  if (!selectedRunId.value && runs.value.length) {
    selectedRunId.value = runs.value[0].run_id
  }
  await loadRunDetail()
}

async function loadRunDetail() {
  attest.value = null
  if (!selectedRunId.value) {
    members.value = { closed_n: 0, member_count: 0, members: [] }
    return
  }
  members.value = await api.reclaimMembers(selectedRunId.value)
  attest.value = await api.reclaimAttest(selectedRunId.value)
}

function selectRun(runId) {
  selectedRunId.value = runId
  loadRunDetail()
}

async function run() {
  busy.value = true
  try {
    report.value = await api.runReclaim()
    selectedRunId.value = report.value.run_id
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
    <p class="sub">按已落地 reclaim_deadline_at 裁决：到期关占用、格口回 free、留核对；未到期不动。关账与趟次成员投影同事务写满，缺一回滚。</p>
    <p>
      <button :disabled="busy" @click="run">跑一轮超时回收扫描</button>
      <span v-if="report" class="sub" style="margin-left:.75rem">
        趟次 <code>{{ report.run_id }}</code> · 扫描时刻 <code>{{ report.scanned_at }}</code>
        · 本次关账 <b>{{ report.reclaimed_count }}</b> 条 · 成员 <b>{{ report.member_count }}</b> 条
        · 补写 deadline {{ report.backfilled_deadlines }} 条
      </span>
    </p>

    <div class="panel">
      <h2 style="margin:0 0 .5rem;font-size:1rem">回收趟次</h2>
      <p v-if="!runs.length" class="sub">暂无回收趟次。</p>
      <table v-else>
        <thead><tr><th>趟次 run_id</th><th>扫描/关账时刻</th><th>closed_n</th><th>补写 deadline</th></tr></thead>
        <tbody>
          <tr v-for="r in runs" :key="r.run_id"
              :style="r.run_id === selectedRunId ? 'background:rgba(56,189,248,.12);cursor:pointer' : 'cursor:pointer'"
              @click="selectRun(r.run_id)">
            <td><code>{{ r.run_id }}</code></td>
            <td>{{ r.scanned_at }}</td>
            <td>{{ r.closed_n }}</td>
            <td>{{ r.backfilled_deadlines }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem">
      <h2 style="margin:0 0 .5rem;font-size:1rem">
        趟次成员投影
        <span v-if="members.run_id" class="sub" style="font-weight:400">— <code>{{ members.run_id }}</code></span>
      </h2>
      <p v-if="!members.run_id" class="sub">尚未产生回收趟次，无成员投影。</p>
      <template v-else>
        <p class="sub">
          closed_n={{ members.closed_n }} · 成员 {{ members.member_count }}
          <span v-if="attest">
            · 核证
            <b :style="attest.valid ? 'color:#34d399' : 'color:#f87171'">
              {{ attest.valid ? '全等通过' : '分叉' }}
            </b>
          </span>
        </p>
        <p v-if="attest" class="sub" style="font-size:.82rem">
          <span v-for="(ok, key) in attest.checks" :key="key" style="margin-right:.9rem">
            {{ ok ? '✓' : '✗' }} {{ key }}
          </span>
        </p>
        <p v-if="attest && !attest.valid" class="sub" style="color:#f87171;font-size:.82rem">
          缺行 {{ attest.missing_member_ids.join(',') || '—' }} ·
          越界 {{ attest.extra_member_ids.join(',') || '—' }} ·
          含未关闭 {{ attest.unclosed_member_ids.join(',') || '—' }}
        </p>
        <table>
          <thead><tr><th>占用 id</th><th>格口</th><th>运单</th><th>回收截止</th><th>关闭时刻</th></tr></thead>
          <tbody>
            <tr v-for="m in members.members" :key="m.occupancy_id">
              <td><code>{{ m.occupancy_id }}</code></td>
              <td>{{ m.cell_id }}</td>
              <td>{{ m.tracking_no }}</td>
              <td>{{ m.reclaim_deadline_at }}</td>
              <td>{{ m.closed_at }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>

    <div class="panel" style="margin-top:1rem">
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
