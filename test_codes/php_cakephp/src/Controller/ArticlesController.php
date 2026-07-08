<?php
declare(strict_types=1);

namespace App\Controller;

class ArticlesController extends AppController
{
    public function initialize(): void
    {
        parent::initialize();

        $this->loadComponent('Billing.Payment');
    }

    public function index(): void
    {
        $articles = $this->fetchTable('Articles');
        $recent = $articles->find()
            ->where(['published' => true])
            ->orderBy(['created' => 'DESC'])
            ->all();

        $this->viewBuilder()->setOption('serialize', ['articles']);
        $this->set('articles', $recent);
    }

    public function view(?string $slug = null): void
    {
        $slug ??= (string)$this->request->getQuery('slug');
        $articles = $this->fetchTable('Articles');
        $article = $articles->findBySlug($slug)->first();

        $this->set(compact('article'));
    }

    public function edit(int $id): void
    {
        $articles = $this->fetchTable('Articles');
        $article = $articles->get($id);
        $article->set('title', (string)$this->request->getData('title'));
        $articles->save($article);

        $trail = $articles->auditTrail($id);
        $note = $this->Payment->capture($id);

        $this->set(compact('article', 'trail', 'note'));
    }

    public function invoices(): void
    {
        $invoices = $this->fetchTable('Billing.Invoices');

        $this->set('invoices', $invoices->find()->all());
    }
}
