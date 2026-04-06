import { useState, useEffect, useCallback } from "react";
import { useParams, useLocation } from "react-router-dom";
import { Download } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { getProject, exportMarkdown } from "../services/api";
import Sidebar from "../components/Sidebar";
import EndpointPage from "../components/EndpointPage";
import SearchBar from "../components/SearchBar";
import { LoadingState, ErrorState } from "../components/States";
import { downloadFile } from "../utils/helpers";
import toast from "react-hot-toast";

/* ─── Component ──────────────────────────────────────────────────────────── */
export default function ProjectViewPage() {
  const { id } = useParams();
  const location = useLocation();
  const [project, setProject] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [activeEndpoint, setActiveEndpoint] = useState(null);

  const fetchProject = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getProject(id);
      setProject(res.data);
      const endpoints = res.data.endpoints || [];
      // If navigated here from global search, jump to that endpoint
      const targetId = location.state?.activeEndpointId;
      const target = targetId ? endpoints.find((e) => e.id === targetId) : null;
      setActiveEndpoint(target || endpoints[0] || null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    fetchProject();
  }, [fetchProject]);

  /* ── Export handler ── */
  const handleExportMD = async () => {
    try {
      const markdown = await exportMarkdown(id);
      downloadFile(markdown, `${project?.name || "api-docs"}.md`);
      toast.success("Markdown exported!");
    } catch {
      toast.error("Export failed");
    }
  };

  /* ── Shared button style helpers ── */
  const btnBase = {
    background: "rgba(36,36,51,0.7)",
    border: "1px solid rgba(255,255,255,0.07)",
  };
  const btnHover = {
    background: "rgba(46,46,64,0.9)",
    borderColor: "rgba(255,255,255,0.1)",
  };

  if (loading) return <LoadingState message="Loading project…" />;
  if (error) return <ErrorState message={error} onRetry={fetchProject} />;
  if (!project) return <ErrorState message="Project not found" />;

  return (
    <div className="flex min-h-screen">
      {/* ── Sidebar ── */}
      <Sidebar
        endpoints={project.endpoints || []}
        activeId={activeEndpoint?.id}
        onSelect={setActiveEndpoint}
        projectName={project.name}
      />

      {/* ── Main content ── */}
      <main className="flex-1 min-w-0">
        {/* Top bar */}
        <header
          className="sticky top-0 z-30 border-b border-white/[0.05]"
          style={{
            background: "rgba(10,10,15,0.82)",
            backdropFilter: "blur(20px)",
          }}
        >
          <div className="flex items-center gap-3 px-4 sm:px-6 lg:px-8 py-3">
            <div className="w-10 lg:hidden" />

            <SearchBar
              projectId={id}
              onSelect={setActiveEndpoint}
              className="flex-1 max-w-md"
            />

            {/* ── Export MD button ── */}
            <motion.button
              onClick={handleExportMD}
              whileTap={{ scale: 0.96 }}
              className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium rounded-lg transition-all duration-200 shrink-0"
              style={btnBase}
              onMouseEnter={(e) => Object.assign(e.currentTarget.style, btnHover)}
              onMouseLeave={(e) => Object.assign(e.currentTarget.style, btnBase)}
            >
              <Download size={14} className="text-text-muted" />
              <span className="hidden sm:inline text-text-secondary">Export MD</span>
            </motion.button>
          </div>
        </header>

        {/* Endpoint content with animated transitions */}
        <div className="px-4 sm:px-6 lg:px-8 py-6 max-w-4xl">
          <AnimatePresence mode="wait">
            <motion.div
              key={activeEndpoint?.id ?? "empty"}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            >
              <EndpointPage endpoint={activeEndpoint} />
            </motion.div>
          </AnimatePresence>
        </div>
      </main>
    </div>
  );
}
