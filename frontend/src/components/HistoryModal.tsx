import { useState, useEffect } from "react";
import type { SessionMeta } from "../types/events";
import { Clock, Trash, X } from "@phosphor-icons/react";

interface HistoryModalProps {
  onClose: () => void;
  onLoad: (id: string) => void;
  onDelete: (id: string) => void;
  listSessions: () => Promise<SessionMeta[]>;
}

export default function HistoryModal({
  onClose,
  onLoad,
  onDelete,
  listSessions,
}: HistoryModalProps) {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listSessions()
      .then(setSessions)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [listSessions]);

  const formatTime = (iso: string) => {
    try {
      const d = new Date(iso);
      return d.toLocaleString();
    } catch {
      return iso;
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-2xl w-[480px] max-h-[70vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[#e5e5e5]">
          <h2 className="text-base font-semibold">Conversation History</h2>
          <button
            onClick={onClose}
            className="p-1 hover:bg-[#f5f5f5] rounded-lg transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-2">
          {loading && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">Loading...</span>
            </div>
          )}

          {!loading && sessions.length === 0 && (
            <div className="flex items-center justify-center py-8">
              <span className="text-sm text-[#999999]">No conversations yet</span>
            </div>
          )}

          {!loading && sessions.length > 0 && (
            <div className="space-y-1">
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center gap-2 px-3 py-2 hover:bg-[#f5f5f5] rounded-lg group"
                >
                  <Clock size={16} className="text-[#999999] shrink-0" />
                  <button
                    onClick={() => onLoad(s.id)}
                    className="flex-1 text-left min-w-0"
                  >
                    <div className="text-sm font-medium truncate">{s.title || "(no title)"}</div>
                    <div className="text-xs text-[#999999]">
                      {formatTime(s.last_message_at)} · {s.message_count} messages
                    </div>
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(s.id);
                      setSessions((prev) => prev.filter((x) => x.id !== s.id));
                    }}
                    className="p-1.5 hover:bg-red-50 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                    title="Delete"
                  >
                    <Trash size={16} className="text-red-500" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
