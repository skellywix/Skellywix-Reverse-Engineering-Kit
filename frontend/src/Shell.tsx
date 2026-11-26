import { useEffect, useRef, useState } from 'react';
import { AppShell } from '@astryxdesign/core/AppShell';
import { Button } from '@astryxdesign/core/Button';
import { Layout, LayoutContent } from '@astryxdesign/core/Layout';
import { MobileNav } from '@astryxdesign/core/MobileNav';
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
} from '@astryxdesign/core/SideNav';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import SkellyHelp from './components/SkellyHelp';
import brandMark from './skellywix-mark.svg?no-inline';

interface NavigationItem {
  label: string;
  href: string;
}

const NAVIGATION_ITEMS: readonly NavigationItem[] = [
  { label: 'Reconstruct', href: '/reconstruct' },
  { label: 'Inspect', href: '/inspect' },
  { label: 'Convert', href: '/convert' },
  { label: 'Runtime', href: '/runtime' },
  { label: 'Releases', href: '/releases' },
];

function useReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)';
  const [isReduced, setIsReduced] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const mediaQuery = window.matchMedia(query);
    const handleChange = (event: MediaQueryListEvent): void => setIsReduced(event.matches);
    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, []);

  return isReduced;
}

interface NavigationLinksProps {
  pathname: string;
  onNavigate: (href: string) => void;
}

function NavigationLinks({ pathname, onNavigate }: NavigationLinksProps): React.JSX.Element {
  return (
    <SideNavSection title="Workspace" isHeaderHidden>
      <SideNavItem label="Build" href="/" size="lg" />
      {NAVIGATION_ITEMS.map((item) => {
        const isSelected =
          pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <SideNavItem
            key={item.href}
            label={item.label}
            href={item.href}
            isSelected={isSelected}
            size="lg"
            onClick={(event) => {
              event.preventDefault();
              onNavigate(item.href);
            }}
          />
        );
      })}
    </SideNavSection>
  );
}

export default function Shell(): React.JSX.Element {
  const location = useLocation();
  const navigate = useNavigate();
  const isReducedMotion = useReducedMotion();
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const drawerWasOpen = useRef(false);

  useEffect(() => {
    if (drawerWasOpen.current && !isDrawerOpen) {
      triggerRef.current?.focus();
    }
    drawerWasOpen.current = isDrawerOpen;
  }, [isDrawerOpen]);

  const handleNavigate = (href: string): void => {
    setIsDrawerOpen(false);
    navigate(href);
  };

  const navigation = (
    <NavigationLinks pathname={location.pathname} onNavigate={handleNavigate} />
  );

  return (
    <section
      className="workspace-shell"
      data-testid="workspace-shell"
      data-reduced-motion={String(isReducedMotion)}
      aria-label="Skellywix workspace"
    >
      <AppShell
        height="fill"
        variant="section"
        contentPadding={0}
        mobileNav={false}
        sideNav={
          <nav className="workspace-navigation" aria-label="Workspace">
            <SideNav
              header={
                <SideNavHeading
                  heading="Skellywix"
                  subheading="Reverse engineering kit"
                  icon={
                    <img
                      className="workspace-brand-mark"
                      src={brandMark}
                      alt=""
                      aria-hidden="true"
                      width="40"
                      height="40"
                    />
                  }
                />
              }
              footer={<SkellyHelp />}
            >
              {navigation}
            </SideNav>
          </nav>
        }
      >
        <header className="mobile-toolbar">
          <Button
            ref={triggerRef}
            label="Open navigation"
            variant="secondary"
            onClick={() => setIsDrawerOpen(true)}
          />
          <span className="mobile-toolbar__title">Skellywix RE Kit</span>
        </header>
        <MobileNav
          isOpen={isDrawerOpen}
          onOpenChange={setIsDrawerOpen}
          header="Skellywix RE Kit"
          label="Workspace navigation"
          side="start"
        >
          <nav className="workspace-drawer-navigation" aria-label="Workspace drawer">
            {navigation}
            <SkellyHelp />
          </nav>
        </MobileNav>
        <Layout height="fill" contentWidth="72rem">
          <LayoutContent label="Workflow" padding={6} isScrollable>
            <section className="page-flow" aria-label="Active workflow">
              <Outlet />
            </section>
          </LayoutContent>
        </Layout>
      </AppShell>
    </section>
  );
}
