<script setup>
import { computed, onMounted, reactive } from "vue"
import {
  Bookmark,
  ExternalLink,
  Inbox,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Trash2,
  Users,
} from "lucide-vue-next"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { EmptyState } from "@/components/ui/empty-state"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"

const TABS = [
  { id: "suggested", label: "Suggested" },
  { id: "keep", label: "Keep" },
  { id: "kill", label: "Kill" },
  { id: "later", label: "Later" },
]

const state = reactive({
  loading: true,
  error: "",
  tab: "suggested",
  decidingKey: "",
  payload: {
    items: [],
    empty: false,
    hint: "",
    counts: { suggested: 0, keep: 0, kill: 0, later: 0, all: 0 },
  },
})

const counts = computed(() => state.payload.counts || {})

function displayName(row) {
  const name = [row.first_name, row.last_name].filter(Boolean).join(" ")
  return name || row.identity_key
}

function titleLine(row) {
  return [row.company, row.position || row.headline].filter(Boolean).join(" · ")
}

function rowKey(row) {
  return `${row.kind}:${row.identity_key}`
}

async function load() {
  state.loading = true
  state.error = ""
  try {
    state.payload = await api.networkSuggestions(state.tab)
  } catch (err) {
    state.error = err.message || "Could not load network suggestions."
  } finally {
    state.loading = false
  }
}

async function chooseTab(tab) {
  state.tab = tab
  await load()
}

async function decide(row, decision) {
  state.decidingKey = rowKey(row)
  state.error = ""
  try {
    await api.setNetworkDecision({
      identity_key: row.identity_key,
      kind: row.kind,
      decision,
    })
    await load()
  } catch (err) {
    state.error = err.message || "Could not save that decision."
  } finally {
    state.decidingKey = ""
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <Alert>
      <ShieldAlert class="h-4 w-4" />
      <AlertDescription>
        Kill means you intend to prune this person. CareerManager does not click
        Remove connection on LinkedIn.
      </AlertDescription>
    </Alert>

    <Card>
      <CardHeader class="flex flex-row items-center justify-between space-y-0">
        <CardTitle>LinkedIn network</CardTitle>
        <Button variant="outline" size="sm" :disabled="state.loading" @click="load">
          <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': state.loading }" />
          Refresh rank
        </Button>
      </CardHeader>
      <CardContent class="space-y-4">
        <div class="flex flex-wrap gap-2">
          <Button
            v-for="tab in TABS"
            :key="tab.id"
            size="sm"
            :variant="state.tab === tab.id ? 'default' : 'outline'"
            @click="chooseTab(tab.id)"
          >
            {{ tab.label }}
            <Badge variant="secondary" class="ml-1">{{ counts[tab.id] || 0 }}</Badge>
          </Button>
        </div>

        <p v-if="state.error" class="text-sm text-destructive">{{ state.error }}</p>

        <div v-if="state.loading" class="space-y-2">
          <Skeleton v-for="n in 6" :key="n" class="h-16 w-full" />
        </div>

        <EmptyState
          v-else-if="state.payload.empty"
          title="No LinkedIn archive imported"
          :description="state.payload.hint || 'Import the official LinkedIn archive first.'"
        >
          <template #icon><Inbox /></template>
        </EmptyState>

        <EmptyState
          v-else-if="!state.payload.items.length"
          :title="`No ${state.tab} people`"
          description="Decisions stay after you refresh rank. Switch tabs to see Keep, Kill, or Later."
        >
          <template #icon><Users /></template>
        </EmptyState>

        <ul v-else class="divide-y divide-border rounded-md border border-border">
          <li
            v-for="row in state.payload.items"
            :key="rowKey(row)"
            class="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between"
          >
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-medium text-foreground">{{ displayName(row) }}</span>
                <Badge variant="warning">{{ row.prune_score }}</Badge>
                <Badge variant="outline">{{ row.kind }}</Badge>
                <a
                  v-if="row.profile_url"
                  :href="row.profile_url"
                  target="_blank"
                  rel="noreferrer"
                  class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                >
                  Profile
                  <ExternalLink class="h-3 w-3" />
                </a>
              </div>
              <p v-if="titleLine(row)" class="text-sm text-muted-foreground">
                {{ titleLine(row) }}
              </p>
              <ul class="list-disc pl-5 text-sm text-muted-foreground">
                <li v-for="reason in row.reasons" :key="reason">{{ reason }}</li>
              </ul>
            </div>
            <div class="flex shrink-0 flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                :disabled="state.decidingKey === rowKey(row)"
                @click="decide(row, 'keep')"
              >
                <Loader2
                  v-if="state.decidingKey === rowKey(row)"
                  class="h-4 w-4 animate-spin"
                />
                <Bookmark v-else class="h-4 w-4" />
                Keep
              </Button>
              <Button
                size="sm"
                variant="destructive"
                :disabled="state.decidingKey === rowKey(row)"
                @click="decide(row, 'kill')"
              >
                <Trash2 class="h-4 w-4" />
                Kill
              </Button>
              <Button
                size="sm"
                variant="ghost"
                :disabled="state.decidingKey === rowKey(row)"
                @click="decide(row, 'later')"
              >
                Later
              </Button>
            </div>
          </li>
        </ul>
      </CardContent>
    </Card>
  </div>
</template>
