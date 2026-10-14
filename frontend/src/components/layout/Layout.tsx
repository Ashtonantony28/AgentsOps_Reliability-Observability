import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ViolationToasts from "@/components/policies/ViolationToasts";

export default function Layout() {
  return (
    <div className="min-h-screen flex bg-ink-950 text-ink-100">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <Header />
        <main className="flex-1 overflow-y-auto scrollbar p-6 min-w-0">
          <Outlet />
        </main>
      </div>
      <ViolationToasts />
    </div>
  );
}
