import { Routes, Route } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import DocsPage from "./pages/DocsPage";
import TopicPage from "./pages/TopicPage";
import SummaryPage from "./pages/SummaryPage";
import ChatPage from "./pages/ChatPage";

export default function App() {
  return (
    <div className="app-layout">
      <Sidebar />
      <Routes>
        <Route path="/" element={<DocsPage />} />
        <Route path="/topic/:name" element={<TopicPage />} />
        <Route path="/summary/:kind/:name" element={<SummaryPage />} />
        <Route path="/chat" element={<ChatPage />} />
      </Routes>
    </div>
  );
}
