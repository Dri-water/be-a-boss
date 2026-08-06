const vscode = require('vscode');

class OrgItem extends vscode.TreeItem {
  constructor(label, collapsibleState, detail, icon, children = []) {
    super(label, collapsibleState);
    this.description = detail;
    this.iconPath = new vscode.ThemeIcon(icon);
    this.children = children;
  }
}

class OrganizationProvider {
  constructor() {
    this.data = null;
    this.error = '';
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  async refresh() {
    const url = vscode.workspace.getConfiguration('beaboss').get('organizationUrl');
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.data = await response.json();
      this.error = '';
    } catch (error) {
      this.error = `Dashboard unavailable: ${error.message}`;
    }
    this.emitter.fire();
  }

  getTreeItem(item) { return item; }

  getChildren(item) {
    if (item) return item.children || [];
    const warnings = this.error ? [new OrgItem(
      this.error,
      vscode.TreeItemCollapsibleState.None,
      this.data ? 'showing last known snapshot' : '',
      'warning',
    )] : [];
    if (!this.data) {
      return warnings.length ? warnings : [new OrgItem(
        'Waiting for organization…', vscode.TreeItemCollapsibleState.None, '', 'loading~spin')];
    }
    const state = value => `${value?.status || value?.runtime || 'dormant'}`;
    const person = (value, icon) => new OrgItem(
      value.name || value.id,
      vscode.TreeItemCollapsibleState.None,
      state(value),
      icon,
    );
    const projectItems = (this.data.projects || []).map(project => {
      const children = [];
      if (project.manager) children.push(person(project.manager, 'organization'));
      children.push(...(project.workers || []).map(worker => person(worker, 'tools')));
      const repos = (project.repos || []).map(value => value.replaceAll('\\', '/').split('/').pop());
      return new OrgItem(
        project.name,
        children.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None,
        `${project.status || 'active'} · ${repos.join(', ')}`,
        'project',
        children,
      );
    });
    const independent = (this.data.independent_workers || []).map(worker => person(worker, 'tools'));
    if (independent.length) {
      projectItems.push(new OrgItem(
        'Independent work', vscode.TreeItemCollapsibleState.Collapsed,
        `${independent.length}`, 'ungroup-by-ref-type', independent,
      ));
    }
    return [...warnings, new OrgItem(
      this.data.orchestrator?.name || 'Orchestrator',
      vscode.TreeItemCollapsibleState.Expanded,
      state(this.data.orchestrator),
      'compass',
      projectItems,
    )];
  }
}

function activate(context) {
  const provider = new OrganizationProvider();
  const view = vscode.window.createTreeView('beaboss.organization', { treeDataProvider: provider });
  let timer;
  const setPolling = visible => {
    if (timer) clearInterval(timer);
    timer = visible ? setInterval(() => provider.refresh(), 3000) : undefined;
    if (visible) provider.refresh();
  };
  setPolling(view.visible);
  context.subscriptions.push(
    view,
    view.onDidChangeVisibility(event => setPolling(event.visible)),
    vscode.commands.registerCommand('beaboss.refreshOrganization', () => provider.refresh()),
    vscode.commands.registerCommand('beaboss.openDashboard', async () => {
      const snapshotUrl = vscode.workspace.getConfiguration('beaboss').get('organizationUrl');
      const dashboardUrl = snapshotUrl.replace(/\/organization\.json(?:\?.*)?$/, '/');
      await vscode.env.openExternal(vscode.Uri.parse(dashboardUrl));
    }),
    { dispose: () => { if (timer) clearInterval(timer); } },
  );
}

function deactivate() {}

module.exports = { activate, deactivate, OrganizationProvider };
