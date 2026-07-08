<?php
declare(strict_types=1);

namespace App\Service;

use Cake\ORM\TableRegistry;

class ReportService
{
    public function latestHeadline(): ?string
    {
        $articles = TableRegistry::getTableLocator()->get('Articles');
        $article = $articles->find()
            ->where(['published' => true])
            ->orderBy(['created' => 'DESC'])
            ->first();

        return $article?->title;
    }

    public function findAuthor(string $email): mixed
    {
        $users = TableRegistry::getTableLocator()->get('Users');

        return $users->findByEmail($email)->first();
    }

    public function invoiceBadge(\Billing\Model\Entity\Invoice $invoice): string
    {
        return $invoice->total_label;
    }
}
