import { useState, useEffect, useCallback } from "react";
import type { MemoryFact, MemorySummary, MemoryPage, MemoryBlock } from "../types/events";
import { Brain, Trash, X, MagnifyingGlass, ChatCircle, HardDrive } from "@phosphor-icons/react";

const API_BASE = "http://127.0.0.1:8000";

interface MemoryModalProps {
  onClose: () => void;
}

type Tab = "facts" | "summaries" | "hot";

export default function MemoryModal({ onClose }: MemoryModalProps) {
  const [tab, setTab] = useState<Tab>("facts");
  const [facts, setFacts] = useState<MemoryFact[]>([]);
  const [summaries, setSummaries] = useState<MemorySummary[]>([]);
  const [blocks, setBlocks] = useState<MemoryBlock[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  const loadFacts = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/memory/facts`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: MemoryPage<MemoryFact> = await res.json();
      setFacts(data.items);
    } catch (e) {
      console.error(e);
    }
  }, []);

  const loadSummaries = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/memory/summaries`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: MemoryPage<MemorySummary> = await res.json();
      setSummaries(data.items);
    } catch (e) {
      console.error(e);
    }
  }, []);

  const loadBlocks = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/memory/blocks`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setBlocks(data.items);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    Promise.all([loadFacts(), loadSummaries(), loadBlocks()])
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [loadFacts, loadSummaries, loadBlocks]);

  const handleDeleteFact = async (doc_id: string) => {
    try {
      const res = await fetch(`${API_BASE}/memory/facts/${doc_id}`, { method: "DELETE" });
      if (res.ok) {
        setFacts((prev) => prev.filter((f) => f.doc_id !== doc_id));
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleDeleteSummary = async (doc_id: string) => {
    try {
      const res = await fetch(`${API_BASE}/memory/summaries/${doc_id}`, { method: "DELETE" });
      if (res.ok) {
        setSummaries((prev) => prev.filter((s) => s.doc_id !== doc_id));
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleDeleteBlock = async (label: string) => {
    try {
      const res = await fetch(`${API_BASE}/memory/blocks/${encodeURIComponent(label)}`, { method: "DELETE" });
      if (res.ok) {
        setBlocks((prev) => prev.filter((b) => b.label !== label));
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      setSearchResult(null);
      return;
    }
    setSearching(true);
    try {
      const res = await fetch(`${API_BASE}/memory/search?query=${encodeURIComponent(searchQuery)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setSearchResult(data.result);
    } catch (e) {
      console.error(e);
    } finally {
      setSearching(false);
    }
  };

  const formatTime = (ts: number) => {
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch {
      return String(ts);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-2xl w-[560px] max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#e5e5e5]">
          <h2 className="text-base font-semibold flex items-center gap-2">
            <Brain size={18} />
            Memory Panel
          </h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[#f5f5f5] rounded-lg transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Search bar */}
        <div className="px-4 py-2 border-b border-[#e5e5e5] flex gap-2">
          <div className="flex-1 flex items-center gap-2 bg-[#f5f5f5] rounded-lg px-3 py-1.5">
            <MagnifyingGlass size={14} className="text-[#999999]" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder="Search memory..."
              className="flex-1 bg-transparent text-sm outline-none"
            />
          </div>
          <button
            onClick={handleSearch}
            disabled={searching}
            className="text-xs bg-[#1a1a1a] text-white px-3 py-1.5 rounded-lg hover:bg-[#333] transition-colors disabled:opacity-50"
          >
            {searching ? "..." : "Search"}
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-[#e5e5e5]">
          <button
            onClick={() => { setTab("facts"); setSearchResult(null); }}
            className={`flex-1 py-2 text-sm font-medium transition-colors ${
              tab === "facts"
                ? "border-b-2 border-[#1a1a1a] text-[#1a1a1a]"
                : "text-[#999999] hover:text-[#666]"
            }`}
          >
            Facts ({facts.length})
          </button>
          <button
            onClick={() => { setTab("summaries"); setSearchResult(null); }}
            className={`flex-1 py-2 text-sm font-medium transition-colors ${
              tab === "summaries"
                ? "border-b-2 border-[#1a1a1a] text-[#1a1a1a]"
                : "text-[#999999] hover:text-[#666]"
            }`}
          >
            Summaries ({summaries.length})
          </button>
          <button
            onClick={() => { setTab("hot"); setSearchResult(null); }}
            className={`flex-1 py-2 text-sm font-medium transition-colors flex items-center justify-center gap-1 ${
              tab === "hot"
                ? "border-b-2 border-[#1a1a1a] text-[#1a1a1a]"
                : "text-[#999999] hover:text-[#666]"
            }`}
          >
            <HardDrive size={14} />
            Hot ({blocks.length})
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {/* Search results */}
          {searchResult && (
            <div className="mb-3 p-3 bg-[#f5f5f5] rounded-lg">
              <div className="text-xs font-medium text-[#666] mb-1">Search Result</div>
              <div className="text-sm text-[#1a1a1a] whitespace-pre-wrap">{searchResult}</div>
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">Loading...</span>
            </div>
          )}

          {!loading && tab === "facts" && facts.length === 0 && !searchResult && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">No facts stored yet</span>
            </div>
          )}

          {!loading && tab === "facts" && facts.map((f) => (
            <div key={f.doc_id} className="group flex items-start gap-2 p-3 bg-[#fafafa] rounded-lg hover:bg-[#f5f5f5] transition-colors">
              <div className="flex-1 min-w-0">
                <div className="text-sm whitespace-pre-wrap">{f.content}</div>
                <div className="text-xs text-[#999] mt-1">
                  {formatTime(f.timestamp)} · {f.source}
                </div>
              </div>
              <button
                onClick={() => handleDeleteFact(f.doc_id)}
                className="p-1.5 hover:bg-red-50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                title="Delete"
              >
                <Trash size={14} className="text-red-500" />
              </button>
            </div>
          ))}

          {!loading && tab === "summaries" && summaries.length === 0 && !searchResult && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">No summaries stored yet</span>
            </div>
          )}

          {!loading && tab === "summaries" && summaries.map((s) => (
            <div key={s.doc_id} className="group flex items-start gap-2 p-3 bg-[#fafafa] rounded-lg hover:bg-[#f5f5f5] transition-colors">
              <ChatCircle size={14} className="text-[#999] mt-0.5 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm whitespace-pre-wrap">{s.content}</div>
                <div className="text-xs text-[#999] mt-1">
                  {formatTime(s.timestamp)} · {s.filepath}
                </div>
              </div>
              <button
                onClick={() => handleDeleteSummary(s.doc_id)}
                className="p-1.5 hover:bg-red-50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                title="Delete"
              >
                <Trash size={14} className="text-red-500" />
              </button>
            </div>
          ))}

          {!loading && tab === "hot" && blocks.length === 0 && !searchResult && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">No hot memory blocks</span>
            </div>
          )}

          {!loading && tab === "hot" && blocks.map((b) => (
            <div key={b.label} className="group flex items-start gap-2 p-3 bg-[#fafafa] rounded-lg hover:bg-[#f5f5f5] transition-colors">
              <div className="flex-1 min-w-0">
                <div className="text-xs font-mono text-[#666] mb-1">[{b.label}]</div>
                <div className="text-sm whitespace-pre-wrap">{b.value}</div>
                <div className="text-xs text-[#999] mt-1">
                  {b.read_only ? "🔒 " : ""}{b.value.length} / {b.limit} chars
                </div>
              </div>
              {!b.read_only && (
                <button
                  onClick={() => handleDeleteBlock(b.label)}
                  className="p-1.5 hover:bg-red-50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                  title="Delete"
                >
                  <Trash size={14} className="text-red-500" />
                </button>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
