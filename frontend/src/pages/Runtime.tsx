import { useCallback } from 'react';
import { Banner } from '@astryxdesign/core/Banner';
import { Heading, Text } from '@astryxdesign/core/Text';
import { Stack } from '@astryxdesign/core/Stack';
import PageHeader from '../components/PageHeader';
import { PrimaryAction, PrimaryActionScope } from '../components/PrimaryAction';
import { createSourcePortClient, useReadResource } from './sourcePortApi';
import type { RecipeState, RuntimeRecipe, RuntimeState, SourcePortClient } from './sourcePortApi';

const defaultClient = createSourcePortClient();

const STATE_TEXT: Record<RecipeState, string> = {
  configured: 'Set up, not tested. No test result is recorded.',
  exercised: 'Test result recorded. See the details below.',
  unavailable: 'Unavailable. This check cannot run.',
  failed: 'Failed. The last check did not pass.',
};

export interface RuntimeProps {
  client?: SourcePortClient;
}

function summarize(recipes: readonly RuntimeRecipe[]): string {
  const count = (state: RecipeState): number =>
    recipes.filter((recipe) => recipe.state === state).length;
  const failed = count('failed');
  return `Set up only: ${count('configured')}. Result recorded: ${count('exercised') + failed}. Unavailable: ${count('unavailable')}. Failed: ${failed}.`;
}

interface Identity {
  label: string;
  value: string;
}

function Fact({ label, value }: Identity): React.JSX.Element {
  return (
    <Stack direction="horizontal" gap={2} wrap="wrap" align="start">
      <Text type="supporting">{label}</Text>
      <code className="workspace-identity-value">{value}</code>
    </Stack>
  );
}

function Recipe({ recipe }: { recipe: RuntimeRecipe }): React.JSX.Element {
  return (
    <Stack as="li" gap={2}>
      <Heading level={2}>{recipe.name}</Heading>
      <Text weight="semibold">{STATE_TEXT[recipe.state]}</Text>
      <Text as="p">{recipe.detail === '' ? 'No detail recorded.' : recipe.detail}</Text>
      <Fact label="Project" value={recipe.project_id} />
      <Fact label="Recipe SHA-256" value={recipe.recipe_sha256} />
      <Fact label="Toolchain SHA-256" value={recipe.toolchain_sha256} />
    </Stack>
  );
}

export default function Runtime({ client = defaultClient }: RuntimeProps): React.JSX.Element {
  const load = useCallback((signal: AbortSignal) => client.getRuntimeState(signal), [client]);
  const { state, reload } = useReadResource<RuntimeState>(load);
  const runtime = state.value;
  const failed = state.phase === 'offline' || state.phase === 'error';
  const isBusy = state.phase === 'loading' || state.isRefreshing;

  return (
    <>
      <PageHeader
        title="Runtime"
        description="See which test tools are set up and which have recorded results. This page does not run tests."
      />
      <Stack gap={4}>
        {state.phase === 'loading' ? <Text color="secondary">Loading runtime state.</Text> : null}
        {failed ? (
          <Banner status="error" role="alert" title={state.message ?? 'The request failed.'} />
        ) : null}
        {runtime !== null && runtime.recipes.length === 0 ? (
          <Text>No test tools are set up.</Text>
        ) : null}
        {runtime !== null && runtime.recipes.length > 0 ? (
          <section aria-label="Verifier recipes">
            <Stack gap={4}>
              <Text as="p">{summarize(runtime.recipes)}</Text>
              <Stack as="ul" gap={4}>
                {runtime.recipes.map((recipe) => (
                  <Recipe key={`${recipe.project_id}|${recipe.recipe_sha256}`} recipe={recipe} />
                ))}
              </Stack>
            </Stack>
          </section>
        ) : null}
        <PrimaryActionScope>
          <PrimaryAction
            label={failed ? 'Retry' : 'Refresh status'}
            isDisabled={isBusy}
            onClick={reload}
          />
        </PrimaryActionScope>
        <output>
          {runtime !== null ? `Runtime state loaded: ${String(runtime.recipes.length)} recipes.` : ''}
        </output>
      </Stack>
    </>
  );
}
