"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/playground", label: "API Playground" },
];

export function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-dot" />
        Sandbox Portal
      </div>
      <div className="brand-sub">Firecracker microVM · demo</div>
      <nav>
        {NAV.map((n) => {
          const active = n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
          return (
            <Link
              key={n.href}
              href={n.href}
              className={`nav-item ${active ? "active" : ""}`}
            >
              {n.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
